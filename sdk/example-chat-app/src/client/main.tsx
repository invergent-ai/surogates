import { ThemeProvider } from "next-themes";
import { createRoot } from "react-dom/client";
import { App } from "./App";
import "./styles.css";

const root = document.getElementById("root");
if (!root) throw new Error("Missing #root element");

// The class strategy, as the web client has it: the theme's tokens and the components' `dark:` utilities
// (and the chart) all follow the one class on <html>, not the operating system.
createRoot(root).render(
  <ThemeProvider attribute="class" defaultTheme="light">
    <App />
  </ThemeProvider>,
);
