async function createJar(event) {
  event.preventDefault();
  const err = document.getElementById("err");
  err.classList.add("hidden");
  const name = document.getElementById("name").value.trim();
  const password = document.getElementById("password").value.trim();
  const reposRaw = document.getElementById("repos").value.trim();
  const payload = { name, agents: [] };
  if (password) payload.password = password;
  if (reposRaw) {
    payload.repos = reposRaw.split(",").map((s) => s.trim()).filter(Boolean);
  }

  try {
    const res = await fetch("/api/jars", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || data.error || "create failed");

    document.getElementById("result").classList.remove("hidden");
    document.getElementById("out-jar").textContent = data.jar;
    document.getElementById("out-pass").textContent = data.password;
    document.getElementById("out-link").textContent = data.share_url;
    const open = document.getElementById("open-link");
    open.href = data.share_url;
    document.getElementById("copy-link").onclick = async () => {
      await navigator.clipboard.writeText(data.share_url);
    };
  } catch (e) {
    err.textContent = e.message || String(e);
    err.classList.remove("hidden");
  }
}

document.getElementById("create-form").addEventListener("submit", createJar);
