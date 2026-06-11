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
  const ADMIN_ONLY = ['/branches'];
  const MANAGER_ONLY = ['/dashboard', '/employees', '/shifts', '/report', '/users', '/settings', '/integrations'];
  const ADMIN_THEME_KEY = 'admin_theme';

  function getStoredAdminTheme() {
    const value = localStorage.getItem(ADMIN_THEME_KEY);
    return value === 'light' ? 'light' : 'dark';
  }

  function applyAdminTheme(theme) {
    const nextTheme = theme === 'light' ? 'light' : 'dark';
    document.documentElement.setAttribute('data-admin-theme', nextTheme);
    window.dispatchEvent(new CustomEvent('admin-theme-change', { detail: { theme: nextTheme } }));
    return nextTheme;
  }

  applyAdminTheme(getStoredAdminTheme());

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

  function saveUser(nextUser) {
    if (!nextUser) return;
    const storage = getAuthStorage();
    storage.setItem('user', JSON.stringify(nextUser));
    user = nextUser;
  }

  function getSelectedBranchId() {
    const user = getUser();
    if (!user || user.role !== 'admin') return '';
    if (document.body && document.body.hasAttribute('data-no-branch-scope')) return '';
    return localStorage.getItem('admin_branch_id') || '';
  }

  function appendBranchParam(url) {
    const branchId = getSelectedBranchId();
    if (!branchId || typeof url !== 'string' || !url.startsWith('/api/')) return url;
    try {
      const parsed = new URL(url, window.location.origin);
      if (!parsed.searchParams.has('branch_id')) {
        parsed.searchParams.set('branch_id', branchId);
      }
      return parsed.pathname + parsed.search + parsed.hash;
    } catch {
      return url;
    }
  }

  function escapeHtml(value) {
    return String(value == null ? '' : value).replace(/[&<>"']/g, function (ch) {
      return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch];
    });
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

  // Kiểm tra role — staff không được vào trang quản lý, manager không vào trang admin-only
  let user = getUser();
  const currentPath = window.location.pathname;

  function isPathIn(paths) {
    return paths.some(function (p) { return currentPath.startsWith(p); });
  }

  function enforcePathAccess(currentUser) {
    if (!currentUser) return false;
    if (isPathIn(ADMIN_ONLY) && currentUser.role !== 'admin') {
      window.location.href = currentUser.role === 'staff' ? '/me' : '/dashboard';
      return true;
    }
    if (currentUser.role === 'staff' && isPathIn(MANAGER_ONLY)) {
      window.location.href = '/me';
      return true;
    }
    return false;
  }

  if (enforcePathAccess(user)) {
    throw new Error('Redirecting unauthorized role');
  }

  // Expose ra global
  window.getToken = getToken;
  window.getUser  = getUser;
  window.getSelectedBranchId = getSelectedBranchId;
  window.scopedApiUrl = appendBranchParam;

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

    const method = String(options.method || 'GET').toUpperCase();
    if (method === 'GET') {
      url = appendBranchParam(url);
    }

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

  window.updateLeaveNavBadge = function (count) {
    const n = Number(count) || 0;
    document.querySelectorAll('a.nav-link[href="/leave"]').forEach(function (link) {
      let badge = link.querySelector('.nav-alert-badge');
      if (!badge) {
        badge = document.createElement('span');
        badge.className = 'nav-alert-badge';
        link.appendChild(badge);
      }
      badge.textContent = n > 99 ? '99+' : String(n);
      badge.classList.toggle('show', n > 0);
      link.setAttribute('aria-label', n > 0 ? 'Nghỉ phép, có ' + n + ' đơn chờ duyệt' : 'Nghỉ phép');
    });
  };

  async function refreshLeaveNavBadge() {
    if (!user || (user.role !== 'admin' && user.role !== 'manager')) return;
    if (!document.querySelector('a.nav-link[href="/leave"]')) return;
    try {
      const res = await window.authFetch('/api/leave/pending-count');
      const data = await res.json();
      window.updateLeaveNavBadge(data.count || 0);
    } catch {}
  }

  async function refreshStoredUser() {
    try {
      const res = await window.authFetch('/auth/me');
      if (!res.ok) return getUser();
      const data = await res.json();
      if (data.user) saveUser(data.user);
      return data.user || getUser();
    } catch {
      return getUser();
    }
  }

  async function installBranchScopeControl() {
    const current = getUser();
    if (!current || !document.body.classList.contains('admin-shell')) return;
    if (document.body.hasAttribute('data-no-branch-scope')) return;
    if (current.role !== 'admin' && current.role !== 'manager') return;
    const header = document.querySelector('body.admin-shell header');
    const account = header ? header.querySelector('.nav-account') : null;
    if (!header || !account || document.querySelector('[data-branch-scope]')) return;
    let branches = [];
    try {
      const res = await window.authFetch('/api/branches');
      branches = await res.json();
      if (!Array.isArray(branches)) branches = [];
    } catch {
      branches = [];
    }
    const scope = current.scope || {};
    const box = document.createElement('div');
    box.className = 'branch-scope-box';
    box.setAttribute('data-branch-scope', '');
    if (current.role === 'admin') {
      const branchRequired = currentPath.startsWith('/shifts') || currentPath.startsWith('/roster');
      const selected = getSelectedBranchId();
      const validSelected = selected && branches.some(function (b) { return String(b.id) === String(selected); });
      if (selected && !validSelected) localStorage.removeItem('admin_branch_id');
      box.innerHTML =
        '<label class="branch-scope-label" for="branchScopeSelect">Chi nhánh</label>' +
        '<select id="branchScopeSelect" class="branch-scope-select">' +
        (branchRequired
          ? '<option value=""' + (validSelected ? '' : ' selected') + ' disabled>Chọn chi nhánh</option>'
          : '<option value="">Tất cả chi nhánh</option>') +
        branches.map(function (b) {
          const isSelected = String(b.id) === String(validSelected ? selected : '');
          return '<option value="' + b.id + '"' + (isSelected ? ' selected' : '') + '>' + escapeHtml(b.name || ('Chi nhánh #' + b.id)) + '</option>';
        }).join('') +
        '</select>';
      header.insertBefore(box, account);
      const select = box.querySelector('select');
      select.addEventListener('change', function () {
        if (select.value) localStorage.setItem('admin_branch_id', select.value);
        else localStorage.removeItem('admin_branch_id');
        window.location.reload();
      });
      return;
    }
    const branchId = scope.branch_id || (scope.branch_ids && scope.branch_ids[0]);
    const branch = branches.find(function (b) { return Number(b.id) === Number(branchId); });
    box.innerHTML =
      '<div class="branch-scope-label">Chi nhánh</div>' +
      '<div class="branch-scope-pill">' + escapeHtml((branch && branch.name) || (branchId ? ('Chi nhánh #' + branchId) : 'Chưa gán')) + '</div>';
    header.insertBefore(box, account);
  }

  function installAdminThemeToggle() {
    if (!document.body.classList.contains('admin-shell')) return;
    if (document.querySelector('[data-admin-theme-toggle]')) return;

    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'theme-toggle-btn admin-theme-floating';
    button.setAttribute('data-admin-theme-toggle', '');

    const syncButton = function () {
      const theme = document.documentElement.getAttribute('data-admin-theme') || getStoredAdminTheme();
      const light = theme === 'light';
      button.setAttribute('aria-pressed', light ? 'true' : 'false');
      button.setAttribute('aria-label', light ? 'Chuyển sang giao diện tối' : 'Chuyển sang giao diện sáng');
      button.setAttribute('title', light ? 'Chuyển sang giao diện tối' : 'Chuyển sang giao diện sáng');
      button.innerHTML =
        '<span class="theme-toggle-icon" aria-hidden="true"></span>' +
        '<span class="theme-toggle-text">' + (light ? 'Sáng' : 'Tối') + '</span>';
    };

    button.addEventListener('click', function () {
      const current = document.documentElement.getAttribute('data-admin-theme') || getStoredAdminTheme();
      const next = current === 'light' ? 'dark' : 'light';
      localStorage.setItem(ADMIN_THEME_KEY, next);
      applyAdminTheme(next);
      syncButton();
    });

    syncButton();
    document.body.appendChild(button);
  }

  // Nút logout nếu có + hiện tên user
  document.addEventListener('DOMContentLoaded', async function () {
    if (document.getElementById('mainNav')) {
      document.body.classList.add('admin-shell');
    }
    await refreshStoredUser();
    if (enforcePathAccess(user)) return;

    const sidebarHeader = document.querySelector('body.admin-shell header');
    if (sidebarHeader && !document.querySelector('[data-sidebar-toggle]')) {
      const toggle = document.createElement('button');
      toggle.type = 'button';
      toggle.className = 'sidebar-collapse-btn';
      toggle.setAttribute('data-sidebar-toggle', '');
      toggle.setAttribute('aria-label', 'Thu gọn thanh điều hướng');
      toggle.innerHTML = '<span aria-hidden="true">‹</span>';
      sidebarHeader.appendChild(toggle);

      const applySidebarState = function (collapsed) {
        document.body.classList.toggle('nav-collapsed', collapsed);
        toggle.setAttribute('aria-expanded', collapsed ? 'false' : 'true');
        toggle.setAttribute('aria-label', collapsed ? 'Mở rộng thanh điều hướng' : 'Thu gọn thanh điều hướng');
      };

      applySidebarState(localStorage.getItem('sidebar_collapsed') === '1');
      toggle.addEventListener('click', function () {
        const collapsed = !document.body.classList.contains('nav-collapsed');
        localStorage.setItem('sidebar_collapsed', collapsed ? '1' : '0');
        applySidebarState(collapsed);
      });
    }
    installAdminThemeToggle();

    // Hiện tên user ở nav nếu có element #navUserName
    const nameEl = document.getElementById('navUserName');
    if (nameEl && user) {
      nameEl.textContent = user.full_name || user.email || '';
    }

    // Ẩn link Tài khoản nếu role không phải admin
    document.querySelectorAll('a.nav-link[href="/branches"]').forEach(function (link) {
      if (user && user.role !== 'admin') link.style.display = 'none';
    });
    const navUsers = document.getElementById('navUsers');
    if (navUsers && user && user.role !== 'admin') {
      navUsers.style.display = 'none';
    }
    const navSettings = document.getElementById('navSettings');
    if (navSettings && user && user.role !== 'admin') {
      navSettings.style.display = 'none';
    }
    const navIntegrations = document.getElementById('navIntegrations');
    if (navIntegrations && user && user.role !== 'admin') {
      navIntegrations.style.display = 'none';
    }
    const navSystemTitle = document.getElementById('navSystemTitle');
    if (navSystemTitle && user && user.role !== 'admin') {
      navSystemTitle.style.display = 'none';
    }
    await installBranchScopeControl();
    refreshLeaveNavBadge();

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
