/**
 * auth-guard.js
 * Include vào đầu mỗi trang protected.
 * - Nếu không có token → redirect login
 * - Expose getToken() và authHeaders() cho toàn trang
 * - Tự động refresh hoặc logout khi token hết hạn (401)
 * - Staff chỉ được truy cập /me, không được vào /dashboard /report /users
 */

(function () {
  const LOGIN_PAGE = '/auth/login-page';
  const MANAGER_ONLY = ['/dashboard', '/report', '/users'];

  function getToken() {
    return localStorage.getItem('access_token') || sessionStorage.getItem('access_token') || '';
  }

  function getRefreshToken() {
    return localStorage.getItem('refresh_token') || sessionStorage.getItem('refresh_token') || '';
  }

  function getAuthStorage() {
    return localStorage.getItem('refresh_token') ? localStorage : sessionStorage;
  }

  function getUser() {
    try { return JSON.parse(localStorage.getItem('user') || sessionStorage.getItem('user') || 'null'); } catch { return null; }
  }

  function getJwtPayload(token) {
    try {
      const payload = token.split('.')[1];
      const normalized = payload.replace(/-/g, '+').replace(/_/g, '/').padEnd(Math.ceil(payload.length / 4) * 4, '=');
      return JSON.parse(atob(normalized));
    } catch {
      return null;
    }
  }

  function isAccessExpired(token, skewSeconds) {
    const payload = getJwtPayload(token);
    if (!payload || !payload.exp) return true;
    return payload.exp * 1000 <= Date.now() + (skewSeconds || 0) * 1000;
  }

  function clearAuth() {
    ['access_token','refresh_token','user'].forEach(k => {
      localStorage.removeItem(k); sessionStorage.removeItem(k);
    });
  }

  function redirectLogin() {
    clearAuth();
    window.location.href = LOGIN_PAGE + '?next=' + encodeURIComponent(window.location.pathname);
  }

  // Kiểm tra role — staff không được vào trang quản lý
  const user = getUser();
  const currentPath = window.location.pathname;
  if (user && user.role === 'staff' && MANAGER_ONLY.some(p => currentPath.startsWith(p))) {
    window.location.href = '/me';
    throw new Error('Redirecting staff to /me');
  }

  // Expose ra global
  window.getToken = getToken;
  window.getUser  = getUser;

  let refreshPromise = null;

  async function refreshAuth() {
    const rt = getRefreshToken();
    if (!rt) return false;

    if (!refreshPromise) {
      refreshPromise = fetch('/auth/refresh', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ refresh_token: rt })
      })
        .then(async function (res) {
          if (!res.ok) return false;
          const data = await res.json();
          if (!data.access_token || !data.refresh_token) return false;
          const storage = getAuthStorage();
          storage.setItem('access_token', data.access_token);
          storage.setItem('refresh_token', data.refresh_token);
          return true;
        })
        .catch(function () { return false; })
        .finally(function () { refreshPromise = null; });
    }

    return refreshPromise;
  }

  async function ensureFreshAccess() {
    const token = getToken();
    if (token && !isAccessExpired(token, 30)) return true;
    return refreshAuth();
  }

  // Kiểm tra ngay khi trang load. Nếu chỉ còn refresh token thì giữ trang lại
  // để authFetch có cơ hội lấy access token mới.
  if (!getToken() && !getRefreshToken()) {
    redirectLogin();
    throw new Error('Redirecting to login');
  }

  window.authHeaders = function (extra) {
    return Object.assign({ 'Authorization': 'Bearer ' + getToken() }, extra || {});
  };

  // Wrapper fetch tự động refresh access token khi gần hết hạn rồi retry 1 lần nếu vẫn gặp 401
  window.authFetch = async function (url, options) {
    options = options || {};
    const fresh = await ensureFreshAccess();
    if (!fresh) {
      redirectLogin();
      throw new Error('Unauthorized');
    }
    options.headers = Object.assign(
      { 'Authorization': 'Bearer ' + getToken() },
      options.headers || {}
    );

    let res = await fetch(url, options);
    if (res.status === 401) {
      const refreshed = await refreshAuth();
      if (refreshed) {
        options.headers = Object.assign({}, options.headers, {
          'Authorization': 'Bearer ' + getToken()
        });
        res = await fetch(url, options);
      }
    }

    if (res.status === 401 || res.status === 403) {
      redirectLogin();
      throw new Error('Unauthorized');
    }
    return res;
  };

  // Nút logout nếu có + hiện tên user
  document.addEventListener('DOMContentLoaded', function () {
    // Hiện tên user ở nav nếu có element #navUserName
    const nameEl = document.getElementById('navUserName');
    if (nameEl && user) {
      nameEl.textContent = user.full_name || user.email || '';
    }

    // Ẩn link Tài khoản nếu role không phải admin
    const navUsers = document.getElementById('navUsers');
    if (navUsers && user && user.role !== 'admin') {
      navUsers.style.display = 'none';
    }

    document.querySelectorAll('[data-logout]').forEach(function (el) {
      el.addEventListener('click', function () {
        const rt = localStorage.getItem('refresh_token') || sessionStorage.getItem('refresh_token');
        if (rt) {
          fetch('/auth/logout', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ refresh_token: rt })
          }).finally(function () { redirectLogin(); });
        } else {
          redirectLogin();
        }
      });
    });
  });
})();
