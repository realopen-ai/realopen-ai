# Stage 1: Build React Application
FROM node:24-alpine AS frontend-builder

WORKDIR /app

COPY frontend/package.json frontend/package-lock.json* ./
RUN npm ci --prefer-offline

COPY frontend/ .
RUN npm run build

# Stage 2: Nginx with Built Frontend
FROM nginx:1.31-alpine

# Remove default config
RUN rm /etc/nginx/conf.d/default.conf

# Copy built frontend from builder stage
COPY --from=frontend-builder /app/dist /usr/share/nginx/html

# Copy nginx configuration
COPY nginx/nginx.conf /etc/nginx/nginx.conf

HEALTHCHECK --interval=30s --timeout=3s --retries=3 \
    CMD wget -qO- http://localhost/ || exit 1

EXPOSE 80

CMD ["nginx", "-g", "daemon off;"]