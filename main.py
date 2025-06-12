import os
import re
import json
import tempfile
from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
from pytube import YouTube
from acrcloud_sdk_python3.acrcloud_recognizer import \
    ACRCloudRecognizer  # Assuming this is the correct import path for the user's package

app = Flask(__name__)
CORS(app)

# ACRCloud Configuration - Loaded from environment variables
ACR_CONFIG = {
    'host': os.environ.get('ACR_HOST'),
    'access_key': os.environ.get('ACR_ACCESS_KEY'),
    'access_secret': os.environ.get('ACR_ACCESS_SECRET'),
    'timeout': 10  # seconds
}

if not all([ACR_CONFIG['host'], ACR_CONFIG['access_key'], ACR_CONFIG['access_secret']]):
    print(
        "Warning: ACRCloud environment variables (ACR_HOST, ACR_ACCESS_KEY, ACR_ACCESS_SECRET) are not fully set. /scan_youtube_audio endpoint will not work.")


def is_valid_youtube_url(url):
    """Validate YouTube URL."""
    regex = r"^(https://)?(www\.)?(youtube\.com/watch\?v=|youtu\.be/)[\w-]+(&\S*)?$"
    return re.match(regex, url)


@app.route('/video_info', methods=['POST'])
def video_info():
    data = request.get_json()
    if not data or 'url' not in data:
        return jsonify({"error": "Missing 'url' in request body"}), 400

    url = data['url']
    if not is_valid_youtube_url(url):
        return jsonify({"error": "Invalid YouTube URL"}), 400

    try:
        yt = YouTube(url)
        info = {
            "title": yt.title,
            "author": yt.author,
            "length": yt.length,
            "views": yt.views,
            "description": yt.description,
            "publish_date": yt.publish_date.isoformat() if yt.publish_date else None,
            "thumbnail_url": yt.thumbnail_url
        }
        return jsonify(info), 200
    except Exception as e:
        return jsonify({"error": f"Error fetching video info: {str(e)}"}), 500


@app.route('/download/<resolution>', methods=['POST'])
def download_video(resolution):
    data = request.get_json()
    if not data or 'url' not in data:
        return jsonify({"error": "Missing 'url' in request body"}), 400

    url = data['url']
    if not is_valid_youtube_url(url):
        return jsonify({"error": "Invalid YouTube URL"}), 400

    temp_dir = tempfile.gettempdir()
    file_path = None

    try:
        yt = YouTube(url)
        stream = None
        if resolution.lower() == 'audio':
            stream = yt.streams.get_audio_only()
            if not stream:
                return jsonify({"error": "No audio stream found"}), 404
            # Pytube often downloads audio as .mp4, let's keep that extension
            filename = f"{yt.video_id}_audio.mp4"
        else:
            stream = yt.streams.filter(res=resolution, progressive=True, file_extension='mp4').first()
            if not stream:
                stream = yt.streams.filter(res=resolution,
                                           file_extension='mp4').first()  # Try non-progressive if progressive not found
            if not stream:
                return jsonify({"error": f"No stream found for resolution {resolution}"}), 404
            filename = f"{yt.video_id}_{resolution}.mp4"

        file_path = os.path.join(temp_dir, filename)
        stream.download(output_path=temp_dir, filename=filename)

        return send_file(file_path, as_attachment=True, download_name=stream.default_filename)
    except Exception as e:
        return jsonify({"error": f"Error downloading video: {str(e)}"}), 500
    finally:
        if file_path and os.path.exists(file_path):
            try:
                os.remove(file_path)
            except Exception as e:
                print(f"Error deleting temporary file {file_path}: {str(e)}")


@app.route('/scan_youtube_audio', methods=['POST'])
def scan_youtube_audio():
    if not all([ACR_CONFIG['host'], ACR_CONFIG['access_key'], ACR_CONFIG['access_secret']]):
        return jsonify({"error": "ACRCloud service is not configured on the server."}), 503

    data = request.get_json()
    if not data or 'url' not in data:
        return jsonify({"error": "Missing 'url' in request body"}), 400

    url = data['url']
    if not is_valid_youtube_url(url):
        return jsonify({"error": "Invalid YouTube URL"}), 400

    temp_audio_path = None
    try:
        yt = YouTube(url)
        audio_stream = yt.streams.get_audio_only()
        if not audio_stream:
            return jsonify({"error": "Could not retrieve audio stream from YouTube video."}), 404

        # Download audio to a temporary file
        temp_dir = tempfile.gettempdir()
        # Pytube often downloads audio as .mp4 with AAC, which is fine for ACRCloud.
        # Using a generic name and letting pytube decide the extension might be safer,
        # but for consistency we can enforce .mp4 if that's what pytube typically gives.
        # audio_filename = audio_stream.default_filename # This includes video title, might be too long
        audio_filename = f"{yt.video_id}_audio_for_scan.mp4"  # More predictable
        temp_audio_path = os.path.join(temp_dir, audio_filename)
        audio_stream.download(output_path=temp_dir, filename=audio_filename)

        # Initialize ACRCloud Recognizer
        recognizer = ACRCloudRecognizer(ACR_CONFIG)

        # Recognize audio file
        # The first parameter is the audio file path, the second is the start time in seconds (0 for beginning)
        # The third parameter (recognize_length) is optional, default is 12 seconds.
        acr_result_raw = recognizer.recognize_by_file(temp_audio_path, 0)
        acr_result_json = json.loads(acr_result_raw)

        return jsonify(acr_result_json), 200

    except Exception as e:
        error_response = {"error": f"Error scanning audio: {str(e)}"}
        # If the result from ACRCloud exists and has a status, include it
        if 'acr_result_json' in locals() and acr_result_json and 'status' in acr_result_json:
            error_response["acr_status"] = acr_result_json['status']
        return jsonify(error_response), 500
    finally:
        if temp_audio_path and os.path.exists(temp_audio_path):
            try:
                os.remove(temp_audio_path)
            except Exception as e:
                print(f"Error deleting temporary audio file {temp_audio_path}: {str(e)}")


if __name__ == '__main__':
    # PORT environment variable is often used by hosting providers like Render
    port = int(os.environ.get('PORT', 8080))
    app.run(debug=False, host='0.0.0.0', port=port)