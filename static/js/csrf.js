/*
 * CSRF protection for the JSON API.
 *
 * Flask-WTF validates an X-CSRFToken header on every state-changing request.
 * Rather than editing each of the ~16 fetch() call sites (and relying on
 * every future one remembering), window.fetch is wrapped once here so the
 * header is attached automatically to same-origin mutating requests.
 */
(function () {
    "use strict";

    var SAFE_METHODS = ["GET", "HEAD", "OPTIONS", "TRACE"];

    function token() {
        var meta = document.querySelector('meta[name="csrf-token"]');
        return meta ? meta.getAttribute("content") : null;
    }

    function isSameOrigin(url) {
        try {
            return new URL(url, window.location.href).origin === window.location.origin;
        } catch (err) {
            return false;
        }
    }

    var originalFetch = window.fetch.bind(window);

    window.fetch = function (resource, options) {
        var opts = options || {};
        var method = (opts.method || "GET").toUpperCase();
        var url = typeof resource === "string" ? resource : (resource && resource.url) || "";

        if (SAFE_METHODS.indexOf(method) === -1 && isSameOrigin(url)) {
            var csrf = token();
            if (csrf) {
                var headers = new Headers(opts.headers || {});
                if (!headers.has("X-CSRFToken")) {
                    headers.set("X-CSRFToken", csrf);
                }
                opts = Object.assign({}, opts, { headers: headers });
            }
            // Session cookie must ride along for the request to be authenticated.
            if (!opts.credentials) {
                opts.credentials = "same-origin";
            }
        }

        return originalFetch(resource, opts);
    };

    /* Logout is a POST so it cannot be triggered by a cross-site link or image. */
    window.logout = function () {
        window.fetch("/api/logout", { method: "POST" })
            .then(function () { window.location.href = "/"; })
            .catch(function () { window.location.href = "/"; });
    };
})();
