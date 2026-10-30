FROM node:22-alpine@sha256:fe4f6d74e20370acb04ad889077d14d0f64a9c8ea6a42bc60cddf1e844699914 AS build
WORKDIR /usr/src/app
COPY --chown=node:node package*.json ./
COPY --chown=node:node . .
RUN yarn install --immutable
RUN yarn build
# Prune devDependencies so that only what is required at runtime is copied
# into the final image.
RUN yarn workspaces focus --all --production
USER node


FROM node:22-alpine@sha256:fe4f6d74e20370acb04ad889077d14d0f64a9c8ea6a42bc60cddf1e844699914

# Keep build and runtime on the same Node major and libc. Apply Alpine security
# updates and retain curl for Compose/Kubernetes health checks.
RUN apk upgrade --no-cache && apk add --no-cache curl \
    && rm -rf /usr/local/lib/node_modules/npm /usr/local/lib/node_modules/corepack /opt/yarn-v* \
    && rm -f /usr/local/bin/npm /usr/local/bin/npx /usr/local/bin/corepack /usr/local/bin/yarn /usr/local/bin/yarnpkg
ENV NODE_ENV=production

WORKDIR /app
USER node

COPY --chown=node:node --from=build /usr/src/app/node_modules ./node_modules
COPY --chown=node:node --from=build /usr/src/app/dist ./dist

ENTRYPOINT [ "node", "dist/main.js" ]
