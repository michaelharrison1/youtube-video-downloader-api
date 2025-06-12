import os
import tempfile
import logging
from flask import Flask, request, jsonify
from flask_cors import CORS
from pytube import YouTube
from pytube.exceptions import (
    VideoUnavailable,
    AgeRestrictedError,
    MembersOnly,
    RecordingUnavailable,
    VideoPrivate,
    LiveStreamError,
    PytubeError  # Generic Pytube exception
)
from acrcloud.recognizer import ACRCloudRecognizer  # Corrected import
import json
import urllib.error  # Explicit import for checking HTTPError

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

app = Flask(__name__)
CORS(app)  # Enable CORS for all routes

# ACRCloud Configuration
acr_config_details = {
    'host': os.environ.get('ACR_CLOUD_HOST') or os.environ.get('ACR_HOST'),
    'access_key': os.environ.get('ACR_CLOUD_ACCESS_KEY') or os.environ.get('ACR_ACCESS_KEY'),
    'access_secret': os.environ.get('ACR_CLOUD_ACCESS_SECRET') or os.environ.get('ACR_ACCESS_SECRET'),
    'timeout': 10  # seconds
}

acr_recognizer = None
if not all([acr_config_details['host'], acr_config_details['access_key'], acr_config_details['access_secret']]):
    logging.warning(
        "ACRCloud configuration is incomplete. Recognition will fail. Please check ACR_CLOUD_HOST/ACR_HOST, ACR_CLOUD_ACCESS_KEY/ACR_ACCESS_KEY, ACR_CLOUD_ACCESS_SECRET/ACR_ACCESS_SECRET env vars.")
else:
    try:
        acr_recognizer = ACRCloudRecognizer(acr_config_details)
        logging.info("ACRCloud Recognizer initialized successfully.")
    except Exception as e:
        logging.error(f"Failed to initialize ACRCloud Recognizer: {e}")


def map_acr_match_to_soundtrace_format(acr_match):
    spotify_data = acr_match.get('external_metadata', {}).get('spotify', {})
    spotify_artist_id = spotify_data.get('artists', [{}])[0].get('id') if spotify_data.get('artists') else None
    spotify_track_id = spotify_data.get('track', {}).get('id') if spotify_data.get('track') else None

    youtube_data = acr_match.get('external_metadata', {}).get('youtube', {})
    youtube_video_id = youtube_data.get('vid') if youtube_data else None

    return {
        'id': acr_match.get('acrid'),
        'title': acr_match.get('title', 'Unknown Title'),
        'artist': ', '.join(
            [artist.get('name', 'Unknown Artist') for artist in acr_match.get('artists', [])]) or 'Unknown Artist',
        'album': acr_match.get('album', {}).get('name', 'Unknown Album'),
        'releaseDate': acr_match.get('release_date', 'N/A'),
        'matchConfidence': acr_match.get('score', 0),
        'spotifyArtistId': spotify_artist_id,
        'spotifyTrackId': spotify_track_id,
        'youtubeVideoId': youtube_video_id,
        'youtubeVideoTitle': acr_match.get('title'),
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
        # Removed 'headers' argument as it's not supported directly
        yt = YouTube(youtube_url, use_oauth=False, allow_oauth_cache=False)

        audio_stream = yt.streams.filter(only_audio=True, abr='128kbps').first()
        if not audio_stream:
            audio_stream = yt.streams.filter(only_audio=True).first()

        if not audio_stream:
            logging.error(f"No audio stream found for URL: {youtube_url}")
            return jsonify({'error': 'No audio stream found for the given YouTube URL.'}), 404

        with tempfile.NamedTemporaryFile(delete=False, suffix=f".{audio_stream.subtype or 'mp4'}") as tmpfile:
            temp_file_path = tmpfile.name
            logging.info(
                f"Downloading audio for {youtube_url} to {temp_file_path} (subtype: {audio_stream.subtype}, type: {audio_stream.type})")
            audio_stream.download(output_path=os.path.dirname(temp_file_path),
                                  filename=os.path.basename(temp_file_path))
        logging.info(f"Download complete: {temp_file_path}, size: {os.path.getsize(temp_file_path)}")

        # 2. Send to ACRCloud
        logging.info(f"Starting ACRCloud recognition for {temp_file_path}")
        scan_results_json_string = acr_recognizer.recognize_by_file(temp_file_path, start_seconds=0, rec_length=12)
        logging.info(f"ACRCloud raw response (first 500 chars): {scan_results_json_string[:500]}...")

        scan_results = json.loads(scan_results_json_string)
        acr_status_code = scan_results.get('status', {}).get('code', -1)
        acr_status_msg = scan_results.get('status', {}).get('msg', 'Unknown ACRCloud status')

        if acr_status_code == 0:  # Success
            matches = [map_acr_match_to_soundtrace_format(music_item) for music_item in
                       scan_results.get('metadata', {}).get('music', [])]
            logging.info(f"ACRCloud success for {youtube_url}. Matches found: {len(matches)}")
            return jsonify({'matches': matches, 'acrCode': acr_status_code, 'acrResponse': acr_status_msg}), 200
        elif acr_status_code == 1001:  # No result
            logging.info(f"ACRCloud no result for {youtube_url}.")
            return jsonify({'matches': [], 'acrCode': acr_status_code, 'acrResponse': acr_status_msg}), 200
        else:  # Other ACRCloud error
            logging.error(f"ACRCloud error for {youtube_url}. Code: {acr_status_code}, Msg: {acr_status_msg}")
            return jsonify(
                {'error': f"ACRCloud recognition error: {acr_status_msg}", 'matches': [], 'acrCode': acr_status_code,
                 'acrResponse': acr_status_msg}), 500

    except (
    VideoUnavailable, AgeRestrictedError, MembersOnly, RecordingUnavailable, VideoPrivate, LiveStreamError) as e_pytube:
        logging.error(f"Pytube - Video specific error for {youtube_url}: {str(e_pytube)}", exc_info=False)
        return jsonify(
            {'error': f'YouTube video error: {str(e_pytube)}', 'acrCode': 9001, 'acrResponse': str(e_pytube)}), 404
    except PytubeError as e_pytube_generic:
        logging.error(f"Pytube - Generic error for {youtube_url}: {str(e_pytube_generic)}", exc_info=True)
        return jsonify({'error': f'YouTube library error: {str(e_pytube_generic)}', 'acrCode': 9002,
                        'acrResponse': str(e_pytube_generic)}), 500
    except Exception as e:
        if isinstance(e, urllib.error.HTTPError):  # Check if it's an HTTPError (likely from Pytube's internal requests)
            error_code_for_response = 9000 + e.code  # Create a custom acrCode based on HTTP status
            if e.code == 400:
                logging.error(f"HTTP Error 400 (Bad Request) from YouTube for {youtube_url}: {str(e)}", exc_info=True)
                return jsonify(
                    {'error': f'YouTube API Bad Request (HTTP 400): {str(e)}', 'acrCode': error_code_for_response,
                     'acrResponse': str(e)}), 502
            elif e.code == 403:
                logging.error(f"HTTP Error 403 (Forbidden) from YouTube for {youtube_url}: {str(e)}", exc_info=True)
                return jsonify(
                    {'error': f'YouTube API Forbidden (HTTP 403): {str(e)}', 'acrCode': error_code_for_response,
                     'acrResponse': str(e)}), 502
            elif e.code == 429:
                logging.error(f"HTTP Error 429 (Too Many Requests) from YouTube for {youtube_url}: {str(e)}",
                              exc_info=True)
                return jsonify({'error': f'Rate limited by YouTube (HTTP 429). Please try again later.',
                                'acrCode': error_code_for_response, 'acrResponse': str(e)}), 429
            else:  # Other HTTP errors from Pytube
                logging.error(f"HTTP Error {e.code} from YouTube for {youtube_url}: {str(e)}", exc_info=True)
                return jsonify(
                    {'error': f'YouTube API Error (HTTP {e.code}): {str(e)}', 'acrCode': error_code_for_response,
                     'acrResponse': str(e)}), 502

        # Fallback for other non-HTTPError, non-Pytube specific exceptions
        logging.error(f"General error processing {youtube_url}: {str(e)}", exc_info=True)
        return jsonify({'error': f'Server error: {str(e)}', 'acrCode': 9500, 'acrResponse': str(e)}), 500
    finally:
        if temp_file_path and os.path.exists(temp_file_path):
            try:
                os.remove(temp_file_path)
                logging.info(f"Temporary file {temp_file_path} deleted.")
            except Exception as e_clean:
                logging.error(f"Error deleting temporary file {temp_file_path}: {str(e_clean)}")


@app.route('/')
def home():
    return "youtube-download API is running. Use POST /api/process-youtube-url endpoint."


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    is_production = os.environ.get('FLASK_ENV') == 'production' or os.environ.get('NODE_ENV') == 'production'
    app.run(host='0.0.0.0', port=port, debug=not is_production)