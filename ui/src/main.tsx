import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { takeToken } from "./api";
import { App } from "./App";
import "./styles.css";

takeToken();
createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
