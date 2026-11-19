FROM node:22-bookworm-slim AS build
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci
COPY . .
ARG SITE_URL
ARG PUBLIC_PRIVACY_EMAIL
ENV SITE_URL=${SITE_URL} PUBLIC_PRIVACY_EMAIL=${PUBLIC_PRIVACY_EMAIL}
RUN test -n "$SITE_URL" && test -n "$PUBLIC_PRIVACY_EMAIL" && npm run build
RUN npm prune --omit=dev

FROM node:22-bookworm-slim AS api
ENV NODE_ENV=production DEPLOY_MODE=production PORT=4321
WORKDIR /app
COPY --from=build --chown=node:node /app/package.json /app/package-lock.json ./
COPY --from=build --chown=node:node /app/node_modules ./node_modules
COPY --from=build --chown=node:node /app/server ./server
COPY --from=build --chown=node:node /app/src/i18n/messages.json ./src/i18n/messages.json
COPY --from=build --chown=node:node /app/dist ./dist
USER node
EXPOSE 4321
CMD ["node", "server/index.mjs"]

FROM nginx:stable-alpine AS web
COPY --from=build /app/dist /usr/share/nginx/html
COPY deploy/nginx-container.conf /etc/nginx/conf.d/default.conf
EXPOSE 8080
