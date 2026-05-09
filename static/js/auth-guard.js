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

  function getUser() {
    try { return JSON.parse(localStorage.getItem('user') || sessionStorage.getItem('user') || 'null'); } catch { return null; }
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

  // Kiểm tra ngay khi trang load
  if (!getToken()) {
    redirectLogin();
    throw new Error('Redirecting to login');
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

  window.authHeaders = function (extra) {
    return Object.assign({ 'Authorization': 'Bearer ' + getToken() }, extra || {});
  };

  // Wrapper fetch tự động xử lý 401
  window.authFetch = async function (url, options) {
    options = options || {};
    options.headers = Object.assign(
      { 'Authorization': 'Bearer ' + getToken() },
      options.headers || {}
    );
    const res = await fetch(url, options);
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
