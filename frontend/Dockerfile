# Stage 1: Build React Application
FROM node:24-alpine AS builder

WORKDIR /app

COPY package.json package-lock.json* ./
RUN npm ci --prefer-offline

COPY . .
RUN npm run build

# Stage 2: Production not used directly (Nginx serves the built files)
# The built output is at /app/dist and will be copied by the Nginx Dockerfile
FROM alpine:3.24

COPY --from=builder /app/dist /dist

# This image is only used to hold the build artifacts
# Nginx Dockerfile copies from this build stage
CMD ["echo", "Use nginx Dockerfile instead"]