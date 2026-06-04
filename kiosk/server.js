const http = require("http");
const fs = require("fs");
const path = require("path");

const root = path.resolve(__dirname, "..");
const kioskTemplate = path.join(root, "templates", "kiosk.html");
const registerTemplate = path.join(root, "templates", "register.html");
const staticRoot = path.join(root, "static");

function loadEnvFile(filePath) {
  if (!fs.existsSync(filePath)) return;
  const content = fs.readFileSync(filePath, "utf8");
  for (const line of content.split(/\r?\n/)) {
    const trimmed = line.trim();
    if (!trimmed || trimmed.startsWith("#")) continue;
    const match = trimmed.match(/^([A-Za-z_][A-Za-z0-9_]*)=(.*)$/);
    if (!match) continue;
    const key = match[1];
    let value = match[2].trim();
    if (
      (value.startsWith('"') && value.endsWith('"')) ||
      (value.startsWith("'") && value.endsWith("'"))
    ) {
      value = value.slice(1, -1);
    }
    if (process.env[key] === undefined) process.env[key] = value;
  }
}

loadEnvFile(path.join(root, ".env"));

const host = process.env.KIOSK_HOST || "127.0.0.1";
const port = Number(process.env.KIOSK_PORT || 5500);
const backend = (process.env.BACKEND_URL || "http://127.0.0.1:8000").replace(/\/+$/, "");
const kioskBranchId = process.env.KIOSK_BRANCH_ID || "";

const mimeTypes = {
  ".html": "text/html; charset=utf-8",
  ".css": "text/css; charset=utf-8",
  ".js": "application/javascript; charset=utf-8",
  ".json": "application/json; charset=utf-8",
  ".png": "image/png",
  ".jpg": "image/jpeg",
  ".jpeg": "image/jpeg",
  ".svg": "image/svg+xml",
  ".ico": "image/x-icon",
  ".woff": "font/woff",
  ".woff2": "font/woff2",
};

function send(res, status, body, contentType = "text/plain; charset=utf-8") {
  res.writeHead(status, {
    "Content-Type": contentType,
    "Cache-Control": "no-store",
  });
  res.end(body);
}

function sendFile(res, filePath) {
  fs.readFile(filePath, (err, body) => {
    if (err) {
      send(res, err.code === "ENOENT" ? 404 : 500, err.code === "ENOENT" ? "Not found" : "Server error");
      return;
    }
    send(res, 200, body, mimeTypes[path.extname(filePath).toLowerCase()] || "application/octet-stream");
  });
}

function renderTemplatePage(req, res, templatePath) {
  fs.readFile(templatePath, "utf8", (err, html) => {
    if (err) {
      send(res, 500, "Cannot read template");
      return;
    }
    const requestUrl = new URL(req.url, `http://${req.headers.host}`);
    if (!requestUrl.searchParams.get("backend")) {
      requestUrl.searchParams.set("backend", backend);
    }
    html = html.replace(
      "<script>",
      `<script>window.KIOSK_BACKEND_URL=${JSON.stringify(requestUrl.searchParams.get("backend"))};window.KIOSK_BRANCH_ID=${JSON.stringify(requestUrl.searchParams.get("branch_id") || kioskBranchId)};</script>\n<script>`,
    );
    send(res, 200, html, "text/html; charset=utf-8");
  });
}

function kioskPage(req, res) {
  renderTemplatePage(req, res, kioskTemplate);
}

function registerPage(req, res) {
  renderTemplatePage(req, res, registerTemplate);
}

const server = http.createServer((req, res) => {
  const requestUrl = new URL(req.url, `http://${req.headers.host}`);
  const pathname = decodeURIComponent(requestUrl.pathname);

  if (pathname === "/" || pathname === "/kiosk.html") {
    kioskPage(req, res);
    return;
  }

  if (pathname === "/register" || pathname === "/register.html") {
    registerPage(req, res);
    return;
  }

  if (pathname.startsWith("/static/")) {
    const relative = pathname.slice("/static/".length);
    const filePath = path.resolve(staticRoot, relative);
    if (!filePath.startsWith(staticRoot + path.sep)) {
      send(res, 403, "Forbidden");
      return;
    }
    sendFile(res, filePath);
    return;
  }

  send(res, 404, "Not found");
});

server.listen(port, host, () => {
  console.log(`Kiosk UI: http://${host}:${port}/`);
  console.log(`Register: http://${host}:${port}/register`);
  console.log(`Backend : ${backend}`);
  console.log(`Branch  : ${kioskBranchId || "CHUA CAU HINH - set KIOSK_BRANCH_ID"}`);
});
