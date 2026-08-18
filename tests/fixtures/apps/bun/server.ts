const server = Bun.serve({
  port: Number(process.env.PORT ?? 3000),
  fetch(request) {
    const healthy = new URL(request.url).pathname === "/health";
    return new Response(healthy ? "healthy\n" : "not found\n", {
      status: healthy ? 200 : 404,
    });
  },
});

console.log(`listening on ${server.port}`);
