---
title: "Nginx Cheatsheet"
sidebar_label: "Cheatsheet"
description: "Quick reference for Nginx configuration, commands, and common scenarios"
sidebar_position: 2
---

# Nginx Cheatsheet

## Installation & Basic Commands

```bash
# Install on Ubuntu/Debian
sudo apt-get update
sudo apt-get install nginx

# Start/Stop/Restart
sudo systemctl start nginx
sudo systemctl stop nginx
sudo systemctl restart nginx
sudo systemctl reload nginx   # Reload config without dropping connections

# Check status
sudo systemctl status nginx

# Test configuration (before applying)
sudo nginx -t

# Check Nginx version
nginx -v

# List loaded modules
nginx -V
```

---

## Configuration Files

```bash
# Main config
/etc/nginx/nginx.conf

# Virtual hosts (site configurations)
/etc/nginx/conf.d/*.conf
/etc/nginx/sites-available/*
/etc/nginx/sites-enabled/*

# Reload after changes
sudo systemctl reload nginx
```

---

## Virtual Host (Server Block) Template

```nginx
server {
    listen 80;
    listen [::]:80;  # IPv6

    server_name example.com www.example.com;

    root /var/www/example.com;
    index index.html index.htm;

    # Logs
    access_log /var/log/nginx/example.com.access.log;
    error_log /var/log/nginx/example.com.error.log;

    location / {
        try_files $uri $uri/ =404;
    }

    # Static files with cache
    location ~* \.(jpg|jpeg|png|gif|ico|css|js)$ {
        expires 30d;
        add_header Cache-Control "public, immutable";
    }
}
```

---

## HTTP to HTTPS Redirect

```nginx
server {
    listen 80;
    server_name example.com www.example.com;
    return 301 https://$server_name$request_uri;
}

server {
    listen 443 ssl http2;
    server_name example.com www.example.com;

    ssl_certificate /etc/ssl/certs/example.com.crt;
    ssl_certificate_key /etc/ssl/private/example.com.key;

    # ... rest of config
}
```

---

## Reverse Proxy

```nginx
upstream backend {
    server backend1.example.com:8080;
    server backend2.example.com:8080;
}

server {
    listen 80;
    server_name example.com;

    location / {
        proxy_pass http://backend;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        # Timeouts
        proxy_connect_timeout 60s;
        proxy_send_timeout 60s;
        proxy_read_timeout 60s;
    }
}
```

---

## Load Balancing Methods

```nginx
upstream backend {
    # Default: round robin
    server server1.example.com;
    server server2.example.com;
}

upstream backend_least_conn {
    # Least connections
    least_conn;
    server server1.example.com;
    server server2.example.com;
}

upstream backend_ip_hash {
    # Same IP always goes to same server
    ip_hash;
    server server1.example.com;
    server server2.example.com;
}

upstream backend_weighted {
    # Weighted distribution
    server server1.example.com weight=5;
    server server2.example.com weight=3;
    server server3.example.com;  # weight=1
}
```

---

## SSL/TLS Configuration

```nginx
server {
    listen 443 ssl http2;
    server_name example.com;

    # Certificates
    ssl_certificate /etc/ssl/certs/example.com.crt;
    ssl_certificate_key /etc/ssl/private/example.com.key;

    # TLS versions (disable old ones)
    ssl_protocols TLSv1.2 TLSv1.3;

    # Strong ciphers
    ssl_ciphers HIGH:!aNULL:!MD5;
    ssl_prefer_server_ciphers on;

    # Session management
    ssl_session_timeout 1d;
    ssl_session_cache shared:SSL:50m;

    # HSTS (force HTTPS for 1 year)
    add_header Strict-Transport-Security "max-age=31536000" always;

    # OCSP stapling (performance)
    ssl_stapling on;
    ssl_stapling_verify on;
    resolver 8.8.8.8 8.8.4.4;
}
```

---

## Caching

```nginx
# Cache storage
proxy_cache_path /var/cache/nginx levels=1:2 keys_zone=my_cache:10m max_size=10g inactive=60d;

server {
    location / {
        proxy_cache my_cache;
        proxy_cache_valid 200 10m;      # Cache 200 responses for 10 min
        proxy_cache_valid 404 1m;       # Cache 404 for 1 min
        proxy_cache_bypass $http_pragma; # Don't cache if pragma header
        proxy_no_cache $http_authorization; # Don't cache if authorized

        # Show cache status in header
        add_header X-Cache-Status $upstream_cache_status;

        proxy_pass http://backend;
    }
}
```

---

## Rate Limiting

```nginx
# Define rate limit zone
limit_req_zone $binary_remote_addr zone=general:10m rate=10r/s;
limit_req_zone $binary_remote_addr zone=api:10m rate=100r/m;

server {
    # Basic rate limit
    location / {
        limit_req zone=general burst=20 nodelay;
        proxy_pass http://backend;
    }

    # API rate limit
    location /api/ {
        limit_req zone=api burst=10 nodelay;
        proxy_pass http://api;
    }

    # Return 429 Too Many Requests when limit exceeded
    limit_req_status 429;
}
```

---

## Security Headers

```nginx
server {
    # Hide version
    server_tokens off;

    # MIME type sniffing prevention
    add_header X-Content-Type-Options "nosniff" always;

    # XSS protection
    add_header X-XSS-Protection "1; mode=block" always;

    # Clickjacking prevention
    add_header X-Frame-Options "SAMEORIGIN" always;

    # Content Security Policy
    add_header Content-Security-Policy "default-src 'self'; script-src 'self' 'unsafe-inline';" always;

    # Referrer Policy
    add_header Referrer-Policy "strict-origin-when-cross-origin" always;
}
```

---

## Compression

```nginx
# Enable Gzip compression
gzip on;
gzip_vary on;
gzip_min_length 1000;        # Compress responses > 1KB
gzip_proxied any;
gzip_types text/plain text/css text/xml text/javascript
           application/x-javascript application/javascript
           application/xml+rss application/json;
gzip_disable "msie6";        # Disable for IE6

# Brotli compression (better than gzip)
brotli on;
brotli_comp_level 6;
brotli_types text/plain text/css application/json application/javascript;
```

---

## Logging

```nginx
# Access log format
log_format main '$remote_addr - $remote_user [$time_local] "$request" '
                '$status $body_bytes_sent "$http_referer" '
                '"$http_user_agent" "$http_x_forwarded_for"';

access_log /var/log/nginx/access.log main;

# Custom format with response time
log_format detailed '$remote_addr - $remote_user [$time_local] '
                    '"$request" $status $body_bytes_sent '
                    '"$http_referer" "$http_user_agent" '
                    'rt=$request_time uct="$upstream_connect_time" '
                    'uht="$upstream_header_time" urt="$upstream_response_time"';

# Disable logging for specific paths
location = /health {
    access_log off;
}

# Log only errors
error_log /var/log/nginx/error.log warn;
```

---

## URL Rewriting

```nginx
# Rewrite URL
location /old-page {
    rewrite ^/old-page(.*)$ /new-page$1 permanent;
}

# Remove file extension
location / {
    try_files $uri $uri/ $uri.html =404;
}

# Route all to index.html (SPA)
location / {
    try_files $uri $uri/ /index.html;
}

# Rewrite based on condition
if ($request_filename !-f) {
    rewrite ^(.*)$ /index.php?url=$1 last;
}

# Redirect with query string preservation
rewrite ^/search/(.*)$ /search.php?q=$1 last;
```

---

## Location Block Directives

```nginx
# Exact match (=)
location = /api {
    # Only matches exactly /api
}

# Prefix match (~)
location ~ ^/api/ {
    # Matches /api/, /api/users, etc. (case sensitive)
}

# Case-insensitive prefix match (~*)
location ~* \.(jpg|jpeg|png|gif)$ {
    # Matches any image file (case insensitive)
}

# Priority order (highest to lowest):
# 1. Exact match (=)
# 2. Prefix match with ^~
# 3. Regular expression (~, ~*)
# 4. Prefix match without ^~
```

---

## Common Issues & Solutions

```nginx
# 502 Bad Gateway - backend unavailable
# Solution: Check backend server status
# upstream server is 0.0.0.0:8080;  # FIX: Use correct IP

# 504 Gateway Timeout - proxy timeout too short
location / {
    proxy_connect_timeout 60s;  # Increase timeouts
    proxy_send_timeout 60s;
    proxy_read_timeout 60s;
}

# Too many redirects
# Solution: Avoid redirect loops with proper conditions
if ($scheme != "https") {
    return 301 https://$server_name$request_uri;  # Correct
}

# Connection refused
# Solution: Check upstream servers are listening
upstream backend {
    server 127.0.0.1:8080;  # Verify this address works
}
```

---

## Performance Tuning

```nginx
# Use sendfile for static files
sendfile on;

# Send data in one packet
tcp_nopush on;

# Disable Nagle's algorithm
tcp_nodelay on;

# Connection timeouts
keepalive_timeout 65;

# Worker connections
events {
    worker_connections 1024;  # Per worker
}

# Buffer sizes
client_body_buffer_size 128k;
client_max_body_size 10m;

# Gzip compression
gzip on;
gzip_min_length 1000;

# Use HTTP/2
listen 443 ssl http2;
```

---

## Status and Monitoring

```bash
# Monitor Nginx in real time
watch 'ps aux | grep nginx'

# Check open connections
netstat -an | grep :80 | wc -l

# Check access logs in real time
tail -f /var/log/nginx/access.log

# Check for errors
tail -f /var/log/nginx/error.log

# Count requests per second
tail -f /var/log/nginx/access.log | wc -l

# Find top 10 IPs by requests
awk '{print $1}' /var/log/nginx/access.log | sort | uniq -c | sort -rn | head -10
```

---

## Quick Reference

```bash
# Reload Nginx safely (no dropped connections)
sudo systemctl reload nginx

# Full restart (may drop connections)
sudo systemctl restart nginx

# Stop Nginx
sudo systemctl stop nginx

# View active connections
sudo ss -tan | grep ESTABLISHED

# Count worker processes
ps aux | grep 'nginx: worker' | wc -l

# Benchmark with ApacheBench
ab -c 100 -n 10000 http://example.com/

# Use curl to test headers
curl -I https://example.com/
```

---

## Debugging Nginx

```bash
# Test configuration syntax
sudo nginx -t

# Verbose test
sudo nginx -T  # Shows parsed config

# Check what processes are running
ps aux | grep nginx

# Check which user Nginx runs as
ps aux | grep 'nginx: master'

# Monitor system resources
top -p $(pgrep -f 'nginx: worker' | tr '\n' ',')

# Check listening ports
sudo ss -tlnp | grep nginx
```
