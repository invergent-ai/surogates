# runtime-office: the agent's editor sessions in headless Chromium.
# Playwright's own image carries the Chromium build playwright-core 1.60.0 drives, with its system libraries.
FROM mcr.microsoft.com/playwright:v1.60.0-noble

LABEL org.opencontainers.image.title="surogates-office" \
      org.opencontainers.image.description="The agent's editor sessions in headless Chromium" \
      org.opencontainers.image.vendor="Invergent SA" \
      org.opencontainers.image.source="https://github.com/invergent-ai/surogates"

# tini: PID 1, reaps the orphans a crashed Chromium leaves behind when it is relaunched
RUN apt-get update && apt-get install -y --no-install-recommends tini \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY runtime-office/package.json runtime-office/package-lock.json ./
RUN npm ci
COPY runtime-office/tsconfig.json runtime-office/tsconfig.build.json ./
COPY runtime-office/src ./src
COPY runtime-office/editor ./editor
RUN npm run build && npm prune --omit=dev

ENV NODE_ENV=production PORT=8080
USER pwuser
EXPOSE 8080
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["node", "dist/main.js"]
