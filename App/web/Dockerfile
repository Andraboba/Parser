# syntax=docker/dockerfile:1
FROM node:24-alpine AS build
WORKDIR /web
COPY package.json package-lock.json ./
RUN npm ci
COPY . .
RUN npm run build

FROM nginx:stable-alpine
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY nginx-common.conf /etc/nginx/nginx-common.conf
COPY --from=build /web/dist /usr/share/nginx/html
