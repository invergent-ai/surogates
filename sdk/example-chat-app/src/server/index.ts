import { loadExampleEnv } from "./env";
import { createExampleApp } from "./app";

loadExampleEnv();

const port = Number(process.env.PORT ?? 8787);
const app = createExampleApp();

app.listen(port, () => {
  console.log(`Example chat backend listening on http://localhost:${port}`);
});
