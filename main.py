from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
from pytube import YouTube
import re
import os
import tempfile  # For handling temporary files for downloads

app = Flask(__name__)
CORS(app)  # Initialize CORS to allow cross-origin requests

# Regular expression to validate YouTube URLs
youtube_regex = (
    r'(https://?www\.youtube\.com/watch\?v=([a-zA-Z0-9_-]+))|'  # Standard watch URL
    r'(https://?youtu\.be/([a-zA-Z0-9_-]+))'  # Shortened youtu.be URL
)


@app.route('/video_info', methods=['POST'])
def get_video_info():
    data = request.get_json()
    if not data or 'url' not in data:
        return jsonify({"error": "Missing 'url' in request body"}), 400

    url = data.get('url')

    if not url or not re.match(youtube_regex, url):
        return jsonify({"error": "Invalid or missing YouTube URL"}), 400

    try:
        yt = YouTube(url)
        video_info = {
            "title": yt.title,
            "author": yt.author,
            "length": yt.length,  # in seconds
            "views": yt.views,
            "description": yt.description,
            "publish_date": yt.publish_date.isoformat() if yt.publish_date else None,
            "thumbnail_url": yt.thumbnail_url,
            "streams": [{"resolution": s.resolution, "mime_type": s.mime_type, "itag": s.itag, "filesize": s.filesize}
                        for s in yt.streams.filter(progressive=True).order_by('resolution').desc()]
        }
        return jsonify(video_info), 200
    except Exception as e:
        app.logger.error(f"Error retrieving video info for {url}: {str(e)}")
        return jsonify({"error": f"Could not retrieve video info: {str(e)}"}), 500


@app.route('/download/<resolution>', methods=['POST'])
def download_video(resolution):
    data = request.get_json()
    if not data or 'url' not in data:
        return jsonify({"error": "Missing 'url' in request body"}), 400

    url = data.get('url')

    if not url or not re.match(youtube_regex, url):
        return jsonify({"error": "Invalid or missing YouTube URL"}), 400

    temp_file_path = None  # Initialize to ensure it's defined for cleanup
    try:
        yt = YouTube(url)
        # Find the stream: exact match for resolution, progressive, and must have audio
        stream = yt.streams.filter(progressive=True, res=resolution, type="video").first()

        if not stream:
            # Fallback: if exact resolution not found, try to find one with audio and video
            # Pytube sometimes lists video-only or audio-only streams as progressive=True.
            # We need to be more specific or guide the user.
            progressive_streams = yt.streams.filter(progressive=True, type="video").order_by('resolution').desc()
            available_resolutions = [s.resolution for s in progressive_streams if s.resolution]

            return jsonify({
                "error": f"Resolution '{resolution}' not available or no suitable progressive stream found for this video.",
                "message": "Progressive streams include both video and audio.",
                "available_progressive_resolutions": list(set(available_resolutions))
            }), 404

        # Sanitize filename from video title to avoid issues with special characters
        base_filename = "".join([c if c.isalnum() or c in [' ', '.', '-'] else '_' for c in yt.title])
        filename_with_ext = f"{base_filename}_{resolution}.{stream.subtype or 'mp4'}"

        # Create a temporary directory to download the file
        # tempfile.gettempdir() provides a system-appropriate temp directory
        temp_dir = tempfile.mkdtemp()
        temp_file_path = os.path.join(temp_dir, filename_with_ext)

        stream.download(output_path=temp_dir, filename=filename_with_ext)

        response = send_file(
            temp_file_path,
            as_attachment=True,
            download_name=filename_with_ext,  # Use the sanitized filename
            mimetype=stream.mime_type
        )

        # Define a cleanup function to remove the temporary file and directory
        @response.call_on_close
        def cleanup_temp_file():
            try:
                if temp_file_path and os.path.exists(temp_file_path):
                    os.remove(temp_file_path)
                # Remove the temporary directory if it's empty
                if temp_dir and os.path.exists(temp_dir) and not os.listdir(temp_dir):
                    os.rmdir(temp_dir)
                elif temp_dir and os.path.exists(temp_dir):  # If not empty, log it - should ideally be empty
                    app.logger.warning(f"Temporary directory {temp_dir} was not empty after file removal.")
            except Exception as e:
                app.logger.error(f"Error cleaning up temp file {temp_file_path} or directory {temp_dir}: {e}")

        return response

    except Exception as e:
        app.logger.error(f"Error downloading video {url} at {resolution}: {str(e)}")
        # Cleanup if a temp file was partially created or directory exists
        if temp_file_path and os.path.exists(temp_file_path):
            try:
                os.remove(temp_file_path)
            except Exception as cleanup_e:
                app.logger.error(f"Error cleaning up temp file {temp_file_path} during exception: {cleanup_e}")
        if 'temp_dir' in locals() and temp_dir and os.path.exists(temp_dir):
            try:
                if not os.listdir(temp_dir):  # Only remove if empty
                    os.rmdir(temp_dir)
            except Exception as cleanup_e:
                app.logger.error(f"Error cleaning up temp directory {temp_dir} during exception: {cleanup_e}")
        return jsonify({"error": f"Could not download video: {str(e)}"}), 500

# The following is commented out because Gunicorn will run the app on Render
# if __name__ == '__main__':
#     # For local development, you might want to specify host and port:
#     # app.run(debug=True, host="0.0.0.0", port=5001)
#     app.run(debug=True)