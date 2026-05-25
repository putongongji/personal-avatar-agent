const visitorLogin = document.querySelector("#visitorLogin");
const adminLogin = document.querySelector("#adminLogin");
const adminEmail = document.querySelector("#adminEmail");
const adminPassword = document.querySelector("#adminPassword");
const authError = document.querySelector("#authError");

function nextPath() {
  const params = new URLSearchParams(window.location.search);
  const next = params.get("next") || "/";
  return next.startsWith("/") ? next : "/";
}

async function postJson(url, payload = {}) {
  const response = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(data.detail || "登录失败");
  }
  return data;
}

visitorLogin.addEventListener("click", async () => {
  authError.textContent = "";
  visitorLogin.disabled = true;
  try {
    await postJson("/api/auth/visitor");
    window.location.href = "/";
  } catch (error) {
    authError.textContent = error.message;
  } finally {
    visitorLogin.disabled = false;
  }
});

adminLogin.addEventListener("submit", async (event) => {
  event.preventDefault();
  authError.textContent = "";
  const button = adminLogin.querySelector("button");
  button.disabled = true;
  try {
    await postJson("/api/auth/admin", {
      email: adminEmail.value,
      password: adminPassword.value,
    });
    window.location.href = nextPath();
  } catch (error) {
    authError.textContent = error.message;
  } finally {
    button.disabled = false;
  }
});
