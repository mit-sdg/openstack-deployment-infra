"use strict";

const http = require("node:http");
const port = Number(process.env.PORT || 8080);

http
  .createServer((request, response) => {
    const healthy = request.url === "/ready";
    response.writeHead(healthy ? 200 : 404, { "content-type": "text/plain" });
    response.end(healthy ? "ready\n" : "not found\n");
  })
  .listen(port, "0.0.0.0", () => console.log(`listening on ${port}`));
