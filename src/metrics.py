import time
import os
from prometheus_client import Counter, Histogram, Gauge, start_http_server

# Start metrics server on a separate port to not interfere with main app
def initialize_metrics(port=8000):
    """Initialize and start the Prometheus metrics server"""
    start_http_server(port)
    print(f"Prometheus metrics server started on port {port}")

# Define metrics (all anonymous - no user tracking)
# Counters
TRANSCRIPTION_COUNT = Counter('transcription_total', 'Total number of transcriptions')
ERROR_COUNT = Counter('transcription_errors_total', 'Total number of transcription errors', ['error_type'])
FILE_COUNT = Counter('audio_files_total', 'Total number of audio files processed')

# Histograms
TRANSCRIPTION_DURATION = Histogram('transcription_seconds', 'Time spent on transcription')
FILE_SIZE = Histogram('file_size_bytes', 'Size of processed files in bytes', 
                      buckets=[1e6, 5e6, 1e7, 5e7, 1e8, 5e8, 1e9])
AUDIO_DURATION = Histogram('audio_duration_seconds', 'Duration of audio files', 
                           buckets=[30, 60, 300, 600, 1800, 3600, 7200])

# Gauges
WORKER_QUEUE_SIZE = Gauge('worker_queue_size', 'Number of files in the processing queue')
PROCESSING_FILE_SIZE = Gauge('processing_file_size_bytes', 'Size of the file currently being processed')

# Helper functions for instrumenting existing code
def track_file_processed(file_path):
    """Track file being processed"""
    FILE_COUNT.inc()
    if os.path.exists(file_path):
        file_size = os.path.getsize(file_path)
        FILE_SIZE.observe(file_size)
        PROCESSING_FILE_SIZE.set(file_size)
    
def track_queue_size(queue_length):
    """Update the queue size metric"""
    WORKER_QUEUE_SIZE.set(queue_length)
    
def track_transcription_error(error_type):
    """Track transcription error by type"""
    ERROR_COUNT.labels(error_type=error_type).inc()
    
def track_audio_duration(duration_seconds):
    """Track audio duration"""
    if duration_seconds and duration_seconds > 0:
        AUDIO_DURATION.observe(duration_seconds)

def time_transcription(func):
    """Decorator to time transcription functions"""
    def wrapper(*args, **kwargs):
        start_time = time.time()
        try:
            result = func(*args, **kwargs)
            TRANSCRIPTION_COUNT.inc()
            return result
        except Exception as e:
            track_transcription_error(type(e).__name__)
            raise
        finally:
            TRANSCRIPTION_DURATION.observe(time.time() - start_time)
            PROCESSING_FILE_SIZE.set(0)  # Reset gauge
    return wrapper
