import { handleRequest } from "./server/http";

const port = Number(Bun.env.PORT ?? 3000);

const server = Bun.serve({
  port,
  fetch: handleRequest,
});

console.log(`Countries Bun full-stack example running at http://localhost:${server.port}`);
