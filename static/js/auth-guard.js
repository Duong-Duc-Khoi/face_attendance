/**
 * auth-guard.js
 * Include vào đầu mỗi trang protected.
 * - Nếu không có token → redirect login
 * - Expose getToken() và authHeaders() cho toàn trang
 * - Tự động refresh hoặc logout khi token hết hạn (401)
 */

(function () {
  const LOGIN_PAGE = '/auth/login-page';

  function getToken() {
    return localStorage.getItem('access_token') || sessionStorage.getItem('access_token') || '';
  }

  function clearAuth() {
    localStorage.removeItem('access_token');
    localStorage.removeItem('refresh_token');
    sessionStorage.removeItem('access_token');
    sessionStorage.removeItem('refresh_token');
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

  // Expose ra global
  window.getToken = getToken;

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

  // Nút logout nếu có
  document.addEventListener('DOMContentLoaded', function () {
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
