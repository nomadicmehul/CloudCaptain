---
title: "Nginx Fundamentals"
sidebar_label: "Fundamentals"
description: "Nginx architecture, configuration, virtual hosts, reverse proxy, load balancing, SSL/TLS, caching, and rate limiting"
sidebar_position: 1
---

# Nginx Fundamentals

## What is Nginx?

Nginx is a lightweight, high-performance HTTP server and reverse proxy. It excels at handling concurrent connections and is commonly used for:

- **Web Server**: Serving static content (HTML, CSS, JavaScript, images)
- **Reverse Proxy**: Forwarding requests to backend applications
- **Load Balancer**: Distributing traffic across multiple servers
- **SSL/TLS Terminator**: Handling HTTPS encryption/decryption
- **Cache Server**: Caching responses to reduce backend load
- **API Gateway**: Rate limiting, request filtering

### Why Nginx Over Apache?

| Feature | Nginx | Apache |
|---------|-------|--------|
| **Architecture** | Event-driven, asynchronous | Process-based, threaded |
| **Memory Usage** | ~10 MB | ~100+ MB |
| **Concurrent Connections** | 10,000+ | Limited by processes |
| **Performance** | High throughput | Good throughput |
| **Configuration** | Simple syntax | Complex |
| **Modularity** | Limited (must recompile) | Extensive |

---

## Nginx Architecture

```
┌──────────────────────────────────────┐
│ Master Process (runs as root)        │
│ - Reads configuration                │
│ - Manages worker processes           │
│ - Handles signals (SIGHUP, SIGTERM)  │
└──────────────────┬───────────────────┘
                   │
      ┌────────────┼────────────┐
      ↓            ↓            ↓
 ┌─────────┐ ┌─────────┐ ┌─────────┐
 │ Worker  │ │ Worker  │ │ Worker  │
 │ Process │ │ Process │ │ Process │
 │ (user)  │ │ (user)  │ │ (user)  │
 └────┬────┘ └────┬────┘ └────┬────┘
      │           │           │
      └───────────┼───────────┘
              (event loop)
     Handles concurrent connections
     Non-blocking I/O
     No threads needed
```

**Key Characteristics:**
- One master process (manages)
- Multiple worker processes (handle requests)
- Event-driven: one worker can handle thousands of connections
- Non-blocking: I/O doesn't block worker process

---

## Basic Configuration

Nginx configuration is in `/etc/nginx/nginx.conf` or included files.

```nginx
# Main context
user nginx;                          # Run as nginx user
worker_processes auto;               # One per CPU core
error_log /var/log/nginx/error.log;

# HTTP context
http {
  # Settings for all HTTP servers
  include /etc/nginx/mime.types;
  default_type application/octet-stream;

  # Logging
  access_log /var/log/nginx/access.log;

  # Performance
  sendfile on;                        # Use kernel copy
  tcp_nopush on;                      # Push data in one packet
  tcp_nodelay on;                     # Send immediately

  # Timeouts
  client_body_timeout 12;
  client_header_timeout 12;
  keepalive_timeout 15;

  # Gzip compression
  gzip on;
  gzip_min_length 1000;

  # Include server configs
  include /etc/nginx/conf.d/*.conf;
}
```

---

## Virtual Hosts

Virtual hosts allow one Nginx instance to serve multiple websites.

```nginx
# /etc/nginx/conf.d/example.com.conf
server {
    listen 80;
    server_name example.com www.example.com;

    # Redirect HTTP to HTTPS
    return 301 https://$host$request_uri;
}

server {
    listen 443 ssl http2;
    server_name example.com www.example.com;

    # SSL certificates
    ssl_certificate /etc/ssl/certs/example.com.crt;
    ssl_certificate_key /etc/ssl/private/example.com.key;

    # Root directory
    root /var/www/example.com/html;
    index index.html;

    # Serve static files
    location / {
        try_files $uri $uri/ =404;
    }

    # Cache static assets
    location ~* \.(jpg|jpeg|png|gif|ico|css|js)$ {
        expires 30d;
        add_header Cache-Control "public, immutable";
    }

    # Proxy dynamic requests to backend
    location /api/ {
        proxy_pass http://backend_servers;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }
}
```

---

## Reverse Proxy & Load Balancing

Forward requests to backend servers and distribute load.

```nginx
# Define upstream backend servers
upstream backend {
    least_conn;  # Load balancing method

    server 10.0.1.10:8080 weight=5;      # 5x weight
    server 10.0.1.11:8080 weight=3;      # 3x weight
    server 10.0.1.12:8080;                # Default weight=1

    # Mark server as backup
    server 10.0.1.13:8080 backup;

    # Connection pooling
    keepalive 32;
}

server {
    listen 80;
    server_name example.com;

    location / {
        proxy_pass http://backend;

        # Pass headers
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

### Load Balancing Methods

| Method | Behavior |
|--------|----------|
| **round_robin** | Each server in turn (default) |
| **least_conn** | Server with fewest connections |
| **ip_hash** | Client IP determines server |
| **random** | Random server |
| **least_time** | Least average response time |

---

## SSL/TLS Termination

Handle HTTPS at the proxy level.

```nginx
server {
    listen 443 ssl http2;
    server_name example.com;

    # SSL Certificates
    ssl_certificate /etc/ssl/certs/example.com.crt;
    ssl_certificate_key /etc/ssl/private/example.com.key;
    ssl_trusted_certificate /etc/ssl/certs/ca-bundle.crt;

    # SSL Configuration
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_ciphers HIGH:!aNULL:!MD5;
    ssl_prefer_server_ciphers on;

    # Session configuration
    ssl_session_timeout 1d;
    ssl_session_cache shared:SSL:50m;
    ssl_session_tickets off;

    # HSTS (force HTTPS)
    add_header Strict-Transport-Security "max-age=31536000; includeSubDomains" always;

    # OCSP Stapling
    ssl_stapling on;
    ssl_stapling_verify on;
    resolver 8.8.8.8;

    location / {
        proxy_pass http://backend;
    }
}
```

---

## Caching

Reduce load on backend by caching responses.

```nginx
# Cache configuration
proxy_cache_path /var/cache/nginx levels=1:2 keys_zone=cache:10m max_size=10g inactive=60d;

server {
    listen 80;
    server_name example.com;

    location / {
        # Enable caching
        proxy_cache cache;
        proxy_cache_key "$scheme$request_method$host$request_uri";

        # Cache successful responses for 30 minutes
        proxy_cache_valid 200 30m;

        # Cache 404s for 5 minutes
        proxy_cache_valid 404 5m;

        # Don't cache if cache control header says so
        proxy_cache_bypass $http_cache_control;
        proxy_no_cache $http_pragma $http_authorization;

        # Show cache status
        add_header X-Cache-Status $upstream_cache_status;

        proxy_pass http://backend;
    }

    # Cache static files longer
    location ~* \.(jpg|jpeg|png|css|js)$ {
        proxy_cache cache;
        proxy_cache_valid 200 90d;
        proxy_pass http://backend;

        # Let client cache for 30 days
        add_header Cache-Control "public, max-age=2592000";
    }
}
```

---

## Rate Limiting

Control request rate per client.

```nginx
# Define rate limit zones
limit_req_zone $binary_remote_addr zone=general:10m rate=10r/s;
limit_req_zone $binary_remote_addr zone=api:10m rate=100r/m;
limit_req_zone $server_name zone=per_server:10m rate=1000r/s;

server {
    listen 80;
    server_name example.com;

    # General rate limit (10 requests/second per IP)
    location / {
        limit_req zone=general burst=20 nodelay;
        proxy_pass http://backend;
    }

    # API rate limit (100 requests/minute per IP)
    location /api/ {
        limit_req zone=api burst=10 nodelay;
        proxy_pass http://api_backend;
    }

    # Different limit for authenticated users
    location /api/premium/ {
        limit_req zone=api burst=100 nodelay;  # Higher burst for premium
        proxy_pass http://api_backend;
    }
}
```

---

## Security Best Practices

```nginx
server {
    # Hide Nginx version
    server_tokens off;

    # Prevent MIME type sniffing
    add_header X-Content-Type-Options "nosniff" always;

    # Enable XSS protection
    add_header X-XSS-Protection "1; mode=block" always;

    # Clickjacking protection
    add_header X-Frame-Options "SAMEORIGIN" always;

    # CSP (Content Security Policy)
    add_header Content-Security-Policy "default-src 'self'; script-src 'self' 'unsafe-inline' cdn.example.com; style-src 'self' 'unsafe-inline';" always;

    # Block disallowed HTTP methods
    location / {
        limit_except GET HEAD POST { deny all; }
    }

    # Log failed auth attempts
    location /admin {
        auth_basic "Restricted";
        auth_basic_user_file /etc/nginx/.htpasswd;

        proxy_pass http://backend;
    }
}
```

---

## Practical Exercises

### Exercise 1: Set Up Basic Web Server

```bash
# 1. Install Nginx
sudo apt-get update
sudo apt-get install nginx

# 2. Create virtual host
sudo tee /etc/nginx/conf.d/mysite.conf > /dev/null <<EOF
server {
    listen 80;
    server_name mysite.local;
    root /var/www/mysite;
    index index.html;

    location / {
        try_files \$uri \$uri/ =404;
    }
}
EOF

# 3. Create content
sudo mkdir -p /var/www/mysite
sudo bash -c 'echo "<h1>Hello Nginx!</h1>" > /var/www/mysite/index.html'

# 4. Test and start
sudo nginx -t
sudo systemctl start nginx
sudo systemctl status nginx

# 5. Verify
curl http://localhost
```

### Exercise 2: Reverse Proxy Setup

```bash
# 1. Create upstream
sudo tee /etc/nginx/conf.d/proxy.conf > /dev/null <<EOF
upstream backend {
    server localhost:3000;
    server localhost:3001;
    server localhost:3002;
}

server {
    listen 80;
    server_name api.local;

    location / {
        proxy_pass http://backend;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
    }
}
EOF

# 2. Test
sudo nginx -t && sudo systemctl reload nginx

# 3. Verify (with backend servers running on 3000, 3001, 3002)
curl http://api.local
```

---

## Key Takeaways

1. **Event-driven**: Nginx handles many connections per process
2. **Configuration is Simple**: Clear structure, easy to understand
3. **Reverse Proxy**: Powerful tool for load balancing and decoupling
4. **TLS Termination**: Offload encryption/decryption from backends
5. **Caching**: Dramatically reduce backend load
6. **Security**: Add headers, rate limit, validate

---

## Further Resources

- [Nginx Official Documentation](https://nginx.org/en/docs/)
- [Nginx Beginner's Guide](https://nginx.org/en/docs/beginners_guide.html)
- [HTTP/2 Configuration](https://nginx.org/en/docs/http/ngx_http_v2_module.html)
