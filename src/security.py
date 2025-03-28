import time
import secrets
import hashlib
import hmac
import base64
import socket
import os
import re
import uuid
import zipfile
from functools import wraps
from typing import Dict, Tuple, Optional
from nicegui import app, ui

# Global storage for rate limiting
# Format: {ip_address: (attempts, last_reset_time)}
rate_limits: Dict[str, Tuple[int, float]] = {}

# ------ PATH AND FILENAME SECURITY ------

def sanitize_filename(filename: str) -> str:
    """
    Sanitize a filename to remove potentially dangerous characters
    
    Args:
        filename: The filename to sanitize
        
    Returns:
        str: A sanitized filename
    """
    # Remove any path components (directory traversal)
    base_name = os.path.basename(filename)
    
    # Replace potentially dangerous characters
    # Only allow alphanumeric, underscore, dash, and dot
    sanitized = re.sub(r'[^\w\-\.]', '_', base_name)
    
    # Ensure the filename doesn't start with a dot (hidden files)
    if sanitized.startswith('.'):
        sanitized = 'f' + sanitized
    
    return sanitized

def safe_path(base_dir: str, user_input: str) -> str:
    """
    Ensure path doesn't escape from base directory (prevent path traversal)
    
    Args:
        base_dir: The base directory that should contain the final path
        user_input: User-provided path component
        
    Returns:
        str: A safe absolute path that's guaranteed to be within base_dir
        
    Raises:
        ValueError: If the resulting path would be outside the base directory
    """
    # Normalize paths to handle different path formats
    base_dir = os.path.normpath(os.path.abspath(base_dir))
    
    # Sanitize the user input first
    safe_input = sanitize_filename(user_input)
    
    # Join paths and normalize the result
    full_path = os.path.normpath(os.path.join(base_dir, safe_input))
    
    # Check if the resulting path is within the base directory
    if not full_path.startswith(base_dir + os.sep) and full_path != base_dir:
        raise ValueError(f"Path traversal attempt detected: {user_input}")
        
    return full_path

# ------ TOKEN GENERATION AND VALIDATION ------

# User session token configuration
SESSION_TOKEN_EXPIRY_DAYS = 7  # 7-day tokens
TOKEN_RENEWAL_THRESHOLD = 0.2  # Renew when less than 20% of time remains

def generate_secure_token() -> str:
    """Generate a cryptographically secure token with expiration timestamp"""
    # Generate 32 bytes (256 bits) of random data for the token ID
    random_bytes = secrets.token_bytes(32)
    token_id = base64.urlsafe_b64encode(random_bytes).decode('utf-8')
    
    # Add expiration timestamp (7 days from now)
    expiry = int(time.time() + (SESSION_TOKEN_EXPIRY_DAYS * 86400))
    
    # Format as id:expiry
    return f"{token_id}:{expiry}"

def sign_token(token: str, secret_key: str) -> str:
    """Add a server-side signature to prevent token tampering"""
    # Use HMAC with SHA-256 for signing
    signature = hmac.new(
        secret_key.encode('utf-8'),
        token.encode('utf-8'),
        hashlib.sha256
    ).digest()
    
    # Combine token and signature with double-colon delimiter to avoid conflicts with token format
    signed_token = base64.urlsafe_b64encode(
        token.encode('utf-8') + b'::' + signature
    ).decode('utf-8')
    
    return signed_token

def validate_token(signed_token: str, secret_key: str) -> Optional[str]:
    """Verify that a token was signed by our server and is not expired"""
    try:
        # Decode the combined token
        decoded = base64.urlsafe_b64decode(signed_token.encode('utf-8'))
        
        # Split into original token and signature using double-colon delimiter
        parts = decoded.split(b'::')
        if len(parts) != 2:
            return None
            
        token_with_expiry, signature = parts
        
        # Verify signature
        expected_signature = hmac.new(
            secret_key.encode('utf-8'),
            token_with_expiry,
            hashlib.sha256
        ).digest()
        
        if not hmac.compare_digest(signature, expected_signature):
            return None
            
        # Extract user_id and expiry
        token_data = token_with_expiry.decode('utf-8').split(':')
        if len(token_data) != 2:
            return None
            
        user_id, expiry_str = token_data
        
        # Check if token has expired
        try:
            expiry = int(expiry_str)
            if time.time() > expiry:
                print(f"Token expired: {expiry_str}")
                return None
        except (ValueError, TypeError):
            print(f"Invalid expiry in token: {expiry_str}")
            return None
            
        return user_id
    except Exception as e:
        print(f"Token validation error: {str(e)}")
        return None

# ------ RATE LIMITING ------

def check_rate_limit(ip_address: str, max_attempts: int = 100, 
                     reset_seconds: int = 3600) -> bool:
    """
    Implement rate limiting for session attempts
    
    Args:
        ip_address: The IP address to check
        max_attempts: Maximum allowed attempts per time window (default: 100)
        reset_seconds: Time window in seconds (default: 1 hour)
        
    Returns:
        bool: True if within rate limit, False if exceeded
    """
    current_time = time.time()
    global rate_limits
    
    if ip_address in rate_limits:
        attempts, last_reset = rate_limits[ip_address]
        # Reset counter if time window passed
        if current_time - last_reset > reset_seconds:
            rate_limits[ip_address] = (1, current_time)
            return True
        # Check if limit exceeded
        elif attempts > max_attempts:
            return False
        else:
            # Increment attempt counter
            rate_limits[ip_address] = (attempts + 1, last_reset)
            return True
    else:
        # First attempt from this IP
        rate_limits[ip_address] = (1, current_time)
        return True

def get_client_ip() -> str:
    """Get the client's IP address from the request context"""
    try:
        # For nicegui's FastAPI integration
        if hasattr(app.storage, 'user') and hasattr(app.storage.user, 'request'):
            if hasattr(app.storage.user.request, 'client'):
                return app.storage.user.request.client.host
    except Exception:
        pass
    # Fallback
    return "127.0.0.1"

# ------ SESSION MANAGEMENT ------

def get_secure_user_id(storage_secret: str, online: bool = True) -> str:
    """
    Get a secure user ID, creating a new one if needed or refreshing if close to expiry
    
    Args:
        storage_secret: The secret key used for signing tokens
        online: Whether the app is running in online mode
        
    Returns:
        str: A validated user ID or a new one if validation fails
    """
    if not online:
        return "local"
        
    # Get client IP for rate limiting
    ip_address = get_client_ip()
    
    # Check rate limit
    if not check_rate_limit(ip_address):
        # Rate limit exceeded - show error but still provide local session
        ui.notify("Too many session attempts. Please try again later.", 
                  color="negative", timeout=5000)
        return "local"
        
    # Check if we have an existing ID
    signed_token = app.storage.browser.get("id", "")
    
    if signed_token != "local":
        try:
            # Decode and examine token structure (without validation yet)
            decoded = base64.urlsafe_b64decode(signed_token.encode('utf-8'))
            parts = decoded.split(b'::')
            
            if len(parts) == 2:
                token_with_expiry = parts[0].decode('utf-8')
                token_parts = token_with_expiry.split(':')
                
                if len(token_parts) == 2:
                    user_id, expiry_str = token_parts
                    
                    try:
                        # Validate the token normally
                        validated_user_id = validate_token(signed_token, storage_secret)
                        if validated_user_id:
                            # Token is valid - check if needs renewal
                            current_time = time.time()
                            expiry = int(expiry_str)
                            token_age = expiry - current_time
                            token_max_age = SESSION_TOKEN_EXPIRY_DAYS * 86400
                            
                            # If less than 20% of validity period remains, refresh token
                            if token_age < (token_max_age * TOKEN_RENEWAL_THRESHOLD):
                                print(f"Refreshing token for {user_id} ({token_age} seconds remaining)")
                                new_expiry = int(current_time + token_max_age)
                                new_token = f"{user_id}:{new_expiry}"
                                signed_token = sign_token(new_token, storage_secret)
                                app.storage.browser["id"] = signed_token
                            
                            return validated_user_id
                    except Exception as e:
                        print(f"Error checking token expiry: {e}")
        except Exception as e:
            print(f"Token inspection error: {e}")
    
    # If we don't have a valid token, generate a new one
    new_token = generate_secure_token()
    signed_token = sign_token(new_token, storage_secret)
    app.storage.browser["id"] = signed_token
    
    # Extract just the user ID part from the new token
    return new_token.split(':')[0]

# ------ SECURITY MIDDLEWARE ------

from starlette.middleware.base import BaseHTTPMiddleware

class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """
    Middleware to add security headers to all responses
    
    This middleware adds various security headers to HTTP responses to improve
    the security posture of the application.
    """
    
    def __init__(self, app, ssl_enabled=False):
        """
        Initialize the middleware
        
        Args:
            app: The FastAPI application
            ssl_enabled: Whether SSL/HTTPS is enabled
        """
        super().__init__(app)
        self.ssl_enabled = ssl_enabled
        
    async def dispatch(self, request, call_next):
        """
        Process each request/response and add security headers
        
        Args:
            request: The incoming HTTP request
            call_next: Function to call the next middleware or route handler
            
        Returns:
            The HTTP response with added security headers
        """
        response = await call_next(request)
        
        # Add security headers
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self' 'unsafe-inline' 'unsafe-eval' blob:; style-src 'self' 'unsafe-inline'; img-src 'self' data:;"
        
        # Cookie security (when using HTTPS)
        if self.ssl_enabled:
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        
        return response

def configure_security_middleware(fastapi_app, ssl_enabled=False):
    """
    Configure security middleware for the FastAPI application
    
    Args:
        fastapi_app: The FastAPI application instance
        ssl_enabled: Whether SSL/HTTPS is enabled
    """
    fastapi_app.add_middleware(SecurityHeadersMiddleware, ssl_enabled=ssl_enabled)
    return fastapi_app

def get_cookie_options(ssl_enabled=False):
    """
    Get secure cookie options for NiceGUI
    
    Args:
        ssl_enabled: Whether SSL/HTTPS is enabled
        
    Returns:
        dict: Cookie options for NiceGUI
    """
    # Configure cookie options when using NiceGUI storage
    cookie_options = {
        "httponly": True,  # Prevents JavaScript access to cookies
        "samesite": "Strict",  # Prevents CSRF attacks
    }
    
    # Add secure flag when using HTTPS
    if ssl_enabled:
        cookie_options["secure"] = True
        
    return cookie_options

# ------ SECURE DOWNLOADS ------

# Storage for download tokens
download_tokens = {}  # token -> (file_path, expiry_time, user_id)
TOKEN_EXPIRY = 3600  # 1 hour default

def generate_download_token(file_path: str, user_id: str, expiry_seconds: int = TOKEN_EXPIRY) -> str:
    """
    Generate a secure, time-limited token for file downloads
    
    Args:
        file_path: Path to the file being downloaded
        user_id: ID of the user requesting the download
        expiry_seconds: Seconds until token expires
        
    Returns:
        str: A secure download token
    """
    # Create a unique token
    token = str(uuid.uuid4())
    
    # Store token with expiry time and file info
    download_tokens[token] = (
        file_path, 
        time.time() + expiry_seconds,
        user_id
    )
    
    print(f"Created download token {token} for {file_path}")
    
    return token

def validate_download_token(token: str) -> Optional[str]:
    """
    Validate a download token and return the associated file path
    
    Args:
        token: The download token to validate
        
    Returns:
        Optional[str]: File path if token is valid, None otherwise
    """
    # Check if token exists
    if token not in download_tokens:
        print(f"Invalid download token: {token}")
        return None
    
    file_path, expiry_time, user_id = download_tokens[token]
    
    # Check if token has expired
    if time.time() > expiry_time:
        # Remove expired token
        del download_tokens[token]
        print(f"Expired download token: {token}")
        return None
    
    # Token is valid - remove it as it should only be used once
    del download_tokens[token]
    
    print(f"Valid download token used: {token} for {file_path}")
    
    return file_path

# ------ ZIP SECURITY ------

def is_safe_zip(zip_path: str, max_size_ratio: int = 100, max_files: int = 1000) -> bool:
    """
    Check if a zip file is safe to extract (not a zip bomb)
    
    Args:
        zip_path: Path to the zip file
        max_size_ratio: Maximum allowed ratio of uncompressed to compressed size
        max_files: Maximum number of files allowed in the archive
        
    Returns:
        bool: True if zip appears safe, False otherwise
    """
    try:
        # Get compressed file size
        compressed_size = os.path.getsize(zip_path)
        if compressed_size == 0:
            print(f"Zero-sized zip file: {zip_path}")
            return False
            
        # Open the zip file
        with zipfile.ZipFile(zip_path, 'r') as zip_ref:
            # Count files and calculate total uncompressed size
            file_count = 0
            uncompressed_size = 0
            
            for info in zip_ref.infolist():
                file_count += 1
                uncompressed_size += info.file_size
                
                # Check file path for traversal attempts
                if '..' in info.filename or info.filename.startswith('/'):
                    print(f"Potentially malicious path in zip: {info.filename}")
                    return False
                    
                # Check if we've exceeded the max file count
                if file_count > max_files:
                    print(f"Zip contains too many files: {file_count}")
                    return False
                
                # Check individual file size ratio only if file is compressed
                if info.compress_size > 0:
                    file_ratio = info.file_size / info.compress_size
                    if file_ratio > max_size_ratio:
                        print(f"Suspicious file in zip: {info.filename} - ratio {file_ratio}")
                        return False
            
            # Check overall ratio
            if compressed_size > 0:
                total_ratio = uncompressed_size / compressed_size
                if total_ratio > max_size_ratio:
                    print(f"Zip bomb detected - ratio {total_ratio}")
                    return False
                
            print(f"Zip file passed security checks: {zip_path} (files: {file_count}, ratio: {uncompressed_size/compressed_size:.1f})")
            return True
    except zipfile.BadZipFile:
        print(f"Not a valid zip file: {zip_path}")
        return False
    except Exception as e:
        print(f"Error checking zip file: {str(e)}")
        return False
