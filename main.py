import os
import tempfile
import logging
from flask import Flask, request, jsonify
from flask_cors import CORS
from pytube import YouTube
from pyacrcloud import ACRCloudRecognizer

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

app = Flask(__name__)
CORS(app)  # Enable CORS for all routes

# ACRCloud Configuration (ensure these are set as environment variables on Render)
acr_config = {
    'host': os.environ.get('ACR_CLOUD_HOST'),
    'access_key': os.environ.get('ACR_CLOUD_ACCESS_KEY'),
    'access_secret': os.environ.get('ACR_CLOUD_ACCESS_SECRET'),
    'timeout': 10  # seconds
}

if not all([acr_config['host'], acr_config['access_key'], acr_config['access_secret']]):
    logging.warning("ACRCloud configuration is incomplete. Recognition will fail.")
    acr_recognizer = None
else:
    acr_recognizer = ACRCloudRecognizer(acr_config)

def map_acr_match_to_soundtrace_format(acr_match):
    """Maps a single ACRCloud music item to the SoundTrace AcrCloudMatch format."""
    spotify_artist_id = acr_match.get('external_metadata', {}).get('spotify', {}).get('artists', [{}])[0].get('id')
    spotify_track_id = acr_match.get('external_metadata', {}).get('spotify', {}).get('track', {}).get('id')
    youtube_video_id = acr_match.get('external_metadata', {}).get('youtube', {}).get('vid')

    return {
        'id': acr_match.get('acrid'),
        'title': acr_match.get('title', 'Unknown Title'),
        'artist': ', '.join([artist.get('name', 'Unknown Artist') for artist in acr_match.get('artists', [])]) or 'Unknown Artist',
        'album': acr_match.get('album', {}).get('name', 'Unknown Album'),
        'releaseDate': acr_match.get('release_date', 'N/A'),
        'matchConfidence': acr_match.get('score', 0),
        'spotifyArtistId': spotify_artist_id,
        'spotifyTrackId': spotify_track_id,
        'youtubeVideoId': youtube_video_id,
        'youtubeVideoTitle': acr_match.get('title'), # Fallback, consider if better source for YT title
        'platformLinks': {
            'spotify': f"https://open.spotify.com/track/{spotify_track_id}" if spotify_track_id else None,
            'youtube': f"https://www.youtube.com/watch?v={youtube_video_id}" if youtube_video_id else None,
        }
    }

@app.route('/api/process-youtube-url', methods=['POST'])
def process_youtube_url():
    logging.info("Received request for /api/process-youtube-url")
    data = request.get_json()
    if not data or 'url' not in data:
        logging.error("Request missing 'url' in JSON payload")
        return jsonify({'error': "Missing 'url' in JSON payload"}), 400

    youtube_url = data['url']
    logging.info(f"Processing URL: {youtube_url}")

    if not acr_recognizer:
        logging.error("ACRCloud recognizer not initialized due to missing config.")
        return jsonify({'error': 'ACRCloud service not configured on server.'}), 500

    temp_file_path = None
    try:
        # 1. Download YouTube audio
        yt = YouTube(youtube_url)
        # Filter for audio streams and get the first one (often Opus or M4A)
        audio_stream = yt.streams.filter(only_audio=True).first()
        if not audio_stream:
            logging.error(f"No audio stream found for URL: {youtube_url}")
            return jsonify({'error': 'No audio stream found for the given YouTube URL.'}), 404

        # Download to a temporary file
        with tempfile.NamedTemporaryFile(delete=False, suffix=f".{audio_stream.subtype or 'mp4'}") as tmpfile:
            temp_file_path = tmpfile.name
            logging.info(f"Downloading audio for {youtube_url} to {temp_file_path}")
            audio_stream.download(output_path=os.path.dirname(temp_file_path), filename=os.path.basename(temp_file_path))
        logging.info(f"Download complete: {temp_file_path}")

        # 2. Send to ACRCloud
        # The recognize_by_file method expects a file path.
        # We can scan for a certain duration, e.g., the first 15 seconds.
        # ACRCloud's free tier usually allows short snippets.
        logging.info(f"Starting ACRCloud recognition for {temp_file_path}")
        # The pyacrcloud library expects start_seconds and rec_length for snippets.
        # If you want to scan the whole file (up to ACRCloud limits), omit these.
        # For snippet scanning, you might choose a default. SoundTrace uses 12s.
        scan_results_json_string = acr_recognizer.recognize_by_file(temp_file_path, start_seconds=0, rec_length=12)
        logging.info(f"ACRCloud raw response: {scan_results_json_string[:500]}...") # Log beginning of response

        import json # Ensure json is imported
        scan_results = json.loads(scan_results_json_string)

        acr_status_code = scan_results.get('status', {}).get('code', -1)
        acr_status_msg = scan_results.get('status', {}).get('msg', 'Unknown ACRCloud status')

        if acr_status_code == 0: # Success
            matches = [map_acr_match_to_soundtrace_format(music_item) for music_item in scan_results.get('metadata', {}).get('music', [])]
            logging.info(f"ACRCloud success for {youtube_url}. Matches found: {len(matches)}")
            return jsonify({
                'matches': matches,
                'acrCode': acr_status_code,
                'acrResponse': acr_status_msg # Or a snippet of the full JSON response
            }), 200
        elif acr_status_code == 1001: # No result
            logging.info(f"ACRCloud no result for {youtube_url}.")
            return jsonify({
                'matches': [],
                'acrCode': acr_status_code,
                'acrResponse': acr_status_msg
            }), 200
        else: # Other ACRCloud error
            logging.error(f"ACRCloud error for {youtube_url}. Code: {acr_status_code}, Msg: {acr_status_msg}")
            return jsonify({
                'error': f"ACRCloud recognition error: {acr_status_msg}",
                'matches': [],
                'acrCode': acr_status_code,
                'acrResponse': acr_status_msg
            }), 500 # Or a more specific status based on ACR code

    except Exception as e:
        logging.error(f"Error processing {youtube_url}: {str(e)}", exc_info=True)
        # Determine if it's a Pytube error or other
        if "HTTP Error 404" in str(e) or "Video unavailable" in str(e): # Pytube specific errors
            return jsonify({'error': f'YouTube video error: {str(e)}'}), 404
        return jsonify({'error': f'Server error: {str(e)}'}), 500
    finally:
        if temp_file_path and os.path.exists(temp_file_path):
            try:
                os.remove(temp_file_path)
                logging.info(f"Temporary file {temp_file_path} deleted.")
            except Exception as e_clean:
                logging.error(f"Error deleting temporary file {temp_file_path}: {str(e_clean)}")

@app.route('/')
def home():
    return "youtube-download API is running. Use /api/process-youtube-url endpoint."

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080)) # Render typically sets PORT
    # For Render, Gunicorn is usually used to run the app, defined in Procfile
    # app.run(host='0.0.0.0', port=port, debug=False) # Debug=False for production
    # If running directly with `python main.py` (e.g. local testing without Gunicorn)
    # set debug=True for easier local development if needed.
    app.run(host='0.0.0.0', port=port)