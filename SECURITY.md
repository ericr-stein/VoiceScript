# Security Considerations for Audio Transcription Application

This document outlines the security features, considerations, and recommendations for the Audio Transcription application.

## Implemented Security Features

### User Session Security
- User sessions are secured with cryptographically signed tokens using HMAC-SHA256
- Rate limiting protects against brute-force attacks and session enumeration
- Session tokens are validated on each request
- Session data is isolated per user with unique user IDs

### File Upload Security
- Filename sanitization prevents path traversal attacks
- Temporary file handling ensures partially uploaded files don't enter processing
- ZIP bomb detection prevents denial-of-service attacks by:
  - Checking compression ratios (uncompressed size / compressed size)
  - Limiting maximum number of files
  - Validating file paths within archives for traversal attempts
- File type validation restricts uploads to audio, video, and safe ZIP files

### Download Security
- One-time-use, expiring download tokens prevent unauthorized access to generated files
- Token-based downloads ensure only authorized users can access specific files
- File existence and content validation before serving prevents empty file attacks
- Tokens expire after 1 hour to limit the attack window

### HTTP Security Headers
- Content-Security-Policy limits resource loading to reduce XSS risks
- X-Frame-Options: DENY prevents clickjacking
- X-Content-Type-Options: nosniff prevents MIME sniffing attacks
- HSTS header enforces HTTPS when enabled

## Configuration Options

### Environment Variables
- `STORAGE_SECRET`: (Required) Used for cryptographic signing of session data
- `SSL_CERTFILE`/`SSL_KEYFILE`: Enable direct HTTPS when not using a reverse proxy
- `ONLINE`: Enable/disable online mode which activates additional security features

### Security Headers
Security headers are added to all HTTP responses via the `SecurityHeadersMiddleware`:
```python
response.headers["X-Content-Type-Options"] = "nosniff"
response.headers["X-Frame-Options"] = "DENY"
response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'"
```

When SSL is enabled, an additional HSTS header is added:
```python
response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
```

## Security Limitations & Future Improvements

### Current Limitations
- The in-memory rate limiting implementation doesn't persist across application restarts
- Use of 'unsafe-inline' in CSP is required for NiceGUI/Quasar but reduces XSS protection
- File-based worker communication requires proper filesystem permissions

### Recommended Improvements
1. **Cookie Security**: Configure cookie security at the reverse proxy (Traefik) level:
   ```yaml
   traefik.http.middlewares.secure-cookies.headers.customresponseheaders.Set-Cookie=SameSite=Strict; Secure; HttpOnly
   ```

2. **Enhanced Rate Limiting**: Implement Redis or database-backed rate limiting for persistence across multiple instances

3. **Client IP Detection**: Enhance client IP detection to properly handle reverse proxies:
   ```python
   def get_client_ip():
       # Check X-Forwarded-For header from trusted proxies
       if 'x-forwarded-for' in request.headers:
           return request.headers['x-forwarded-for'].split(',')[0].strip()
       return request.client.host
   ```

4. **Content Security Policy**: Consider using nonces or hashes instead of 'unsafe-inline' for scripts and styles

5. **File I/O**: Use async file operations for better performance and responsiveness:
   ```python
   from nicegui.globals import run_sync
   
   async def read_files():
       return await run_sync(lambda: os.listdir(directory))
   ```

6. **Docker Security**: Run as non-root user and implement resource limits:
   ```dockerfile
   RUN groupadd -r appuser && useradd -r -g appuser appuser
   USER appuser
   ```

## Secure Development Practices

### File Operations
- Always validate and sanitize user-provided filenames
- Use temporary files for uploads until security checks complete
- Verify file content before processing or serving

### Authentication & Session Management
- Generate and validate tokens cryptographically
- Implement rate limiting to prevent brute force attacks
- Use short-lived, single-use tokens for sensitive operations

### Configuration & Deployment
- Use environment variables for sensitive configuration
- Set proper filesystem permissions on data directories
- Configure a reverse proxy with TLS termination and security headers

## Security Incident Response

If you discover a security vulnerability in this application, please follow these steps:

1. **Do not disclose the vulnerability publicly** until it has been addressed
2. Report the issue to the maintainers with detailed information
3. Allow time for the vulnerability to be fixed before public disclosure

## Maintenance & Updates

This security document should be reviewed and updated whenever:
- New features are added to the application
- Security vulnerabilities are identified and fixed
- The threat landscape changes
- New best practices emerge

Last Updated: March 28, 2025
