import os
import time
import shutil
import threading
from os.path import join, getmtime, exists

# Configuration
INACTIVE_DAYS = 7  # Remove directories older than 7 days
CLEANUP_INTERVAL_HOURS = 24  # Run once per day
CLEANUP_ENABLED = True  # Set to False to disable cleanup


def cleanup_inactive_directories(root_dir):
    """
    Remove user directories that haven't been accessed recently
    
    Args:
        root_dir: The root directory of the application
    """
    if not CLEANUP_ENABLED:
        return
           
    print(f"Starting cleanup of inactive directories (threshold: {INACTIVE_DAYS} days)")
       
    # User directory paths
    base_dirs = [
        join(root_dir, "data", "in"),
        join(root_dir, "data", "out"),
        join(root_dir, "data", "error"),
        join(root_dir, "data", "worker")
    ]
       
    # Current time and threshold
    current_time = time.time()
    inactive_threshold = current_time - (INACTIVE_DAYS * 86400)
       
    processed_users = set()
    removed_count = 0
       
    # Check each base directory
    for base_dir in base_dirs:
        if not exists(base_dir):
            continue
               
        # Check each user directory
        try:
            for user_id in os.listdir(base_dir):
                # Skip if already processed or "local" user
                if user_id in processed_users or user_id == "local":
                    continue
                       
                user_dir = join(base_dir, user_id)
                if not os.path.isdir(user_dir):
                    continue
                   
                # Get latest activity time
                try:
                    latest_time = 0
                    for root, dirs, files in os.walk(user_dir):
                        for file in files:
                            file_path = join(root, file)
                            try:
                                mtime = getmtime(file_path)
                                latest_time = max(latest_time, mtime)
                            except Exception:
                                pass
                       
                    # If no files found, use directory time
                    if latest_time == 0:
                        latest_time = getmtime(user_dir)
                       
                    # Check if inactive
                    if latest_time < inactive_threshold:
                        # Remove all directories for this user
                        for base in base_dirs:
                            user_path = join(base, user_id)
                            if exists(user_path):
                                shutil.rmtree(user_path)
                                formatted_time = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(latest_time))
                                print(f"Removed inactive user directory: {user_path} (last activity: {formatted_time})")
                           
                        removed_count += 1
                       
                    # Mark as processed
                    processed_users.add(user_id)
                except Exception as e:
                    print(f"Error checking {user_dir}: {e}")
        except Exception as e:
            print(f"Error scanning {base_dir}: {e}")
       
    print(f"Cleanup completed: removed {removed_count} inactive user directories")


def start_cleanup_thread(root_dir):
    """
    Start background cleanup thread
    
    Args:
        root_dir: The root directory of the application
    """
    if not CLEANUP_ENABLED:
        print("Directory cleanup is disabled")
        return
           
    def cleanup_task():
        # Initial delay
        time.sleep(60)  # Wait 1 minute after startup
           
        while True:
            try:
                cleanup_inactive_directories(root_dir)
            except Exception as e:
                print(f"Error in cleanup task: {e}")
                   
            # Wait until next interval
            time.sleep(CLEANUP_INTERVAL_HOURS * 3600)
       
    # Start daemon thread
    thread = threading.Thread(target=cleanup_task, daemon=True)
    thread.start()
    print(f"Started directory cleanup thread (interval: {CLEANUP_INTERVAL_HOURS}h, threshold: {INACTIVE_DAYS}d)")
