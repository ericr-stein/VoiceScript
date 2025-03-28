# Security Improvements for Audio Transcription System

This document details the security improvements made to the Audio Transcription System, along with deployment instructions.

## Changes Implemented

### 1. Path Prefix Removal for Improved URL Structure

We've removed the `/secure` path prefix from the application, simplifying the URL structure and fixing 404 issues:

- **Main application**: Now served directly at the root of the hostname (`https://sp000200-t6.kt.ktzh.ch/`)
- **Monitoring**: Still served at dedicated paths (`/prometheus` and `/grafana`)

#### Files Changed:
- `main.py`: Removed BASE_PATH variable and references
- `docker-compose.yml`: Updated Traefik routing configuration

### 2. Enhanced File Security

Added security checks to prevent various types of attacks:

- **Filename Sanitization**: All filenames are properly sanitized to prevent path traversal
- **ZIP-bomb Protection**: Added safeguards against ZIP bombs and malicious archives
- **Path Validation**: Implemented strict path validation to prevent directory traversal

### 3. Secure Downloads

- **Token-based Downloads**: Implemented secure, time-limited tokens for file downloads
- **One-time-use Tokens**: Download tokens can only be used once
- **Content-Disposition**: Proper headers ensure downloads work correctly

### 4. User Session Management

- **Token Expiration**: Session tokens now expire after 7 days
- **Auto-renewal**: Tokens are automatically renewed when less than 20% of lifetime remains
- **Signature Verification**: Token validation includes cryptographic signature verification

### 5. Automatic Directory Cleanup

Added an automated cleanup system that:
- Removes inactive user directories after 7 days
- Runs once per day on a background thread
- Preserves system resources by removing unused files

## Deployment Instructions

Follow these steps to deploy the updated system:

1. **Update Application Code**:
   - Deploy the updated `main.py` file
   - Deploy the new `src/security.py` file
   - Deploy the new `src/cleanup.py` file

2. **Update Docker Configuration**:
   - Deploy the updated `docker-compose.yml` file

3. **Rebuild and Restart Containers**:
   ```bash
   # Stop the current containers
   docker-compose down
   
   # Rebuild the containers with the new configuration
   docker-compose build --no-cache
   
   # Start the containers
   docker-compose up -d
   ```

4. **Verify Deployment**:
   - Check that the application is accessible at `https://sp000200-t6.kt.ktzh.ch/`
   - Verify that Prometheus is accessible at `https://sp000200-t6.kt.ktzh.ch/prometheus`
   - Verify that Grafana is accessible at `https://sp000200-t6.kt.ktzh.ch/grafana`

5. **Test Security Features**:
   - Verify file uploads and downloads work correctly
   - Check editor functionality for transcribed files
   - Test media playback in the editor

## Configuration Options

### Directory Cleanup

You can adjust the cleanup settings in `src/cleanup.py`:

```python
# Default settings
INACTIVE_DAYS = 7  # Remove directories older than 7 days
CLEANUP_INTERVAL_HOURS = 24  # Run once per day
CLEANUP_ENABLED = True  # Set to False to disable cleanup
```

### Security Settings

Session token expiration can be configured in `src/security.py`:

```python
# User session token configuration
SESSION_TOKEN_EXPIRY_DAYS = 7  # 7-day tokens
TOKEN_RENEWAL_THRESHOLD = 0.2  # Renew when less than 20% of time remains
```

Download token expiration:

```python
TOKEN_EXPIRY = 3600  # 1 hour default for download tokens
```

## Monitoring

Monitor the system logs for any security-related events:

```bash
docker logs -f audiotranscription-secure
```

Look for these events in the logs:
- "Token expired" - Indicates a user session expired
- "Sanitized filename" - Shows filename sanitization in action
- "Removed inactive user directory" - Confirms cleanup is working
- "Downloaded token created/used" - Track download token usage

## Troubleshooting

1. **404 Errors for Media Files**:
   - Check the console for details on which files are failing to load
   - Verify the mounting of the static data directory

2. **Authentication Issues**:
   - Ensure Traefik is properly configured for basic auth
   - Check that the auth header is being passed correctly

3. **Empty File Directories**:
   - If user directories are being cleaned up too aggressively, adjust the `INACTIVE_DAYS` setting in `src/cleanup.py`

## Security Best Practices

Additional security measures that should be maintained:

1. **Keep Credentials Secure**:
   - Store passwords and secrets in environment variables
   - Use `.env` files for local development only

2. **Regular Updates**:
   - Keep container images updated
   - Apply security patches promptly

3. **Access Control**:
   - Maintain the secure Basic Auth for administrative endpoints
   - Consider implementing role-based access control for more granular permissions
