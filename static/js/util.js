/*
 * Shared view helpers.
 */
(function () {
    "use strict";

    var ENTITIES = {
        "&": "&amp;",
        "<": "&lt;",
        ">": "&gt;",
        '"': "&quot;",
        "'": "&#39;"
    };

    /*
     * Escape a value before it is interpolated into an innerHTML template.
     * Usernames are chosen by users, so rendering one unescaped into the
     * leaderboard would let any account store script that runs in every
     * other user's browser.
     */
    window.escapeHtml = function (value) {
        if (value === null || value === undefined) {
            return "";
        }
        return String(value).replace(/[&<>"']/g, function (ch) {
            return ENTITIES[ch];
        });
    };

    /*
     * Monetary values arrive from the API as strings, not JSON numbers, so
     * that the exact decimal digits survive the trip (JSON.parse would turn
     * a number into a float64 and quietly round it).
     *
     * Presentation is the one place converting to a JS number is safe: these
     * helpers exist so that conversion happens here, at the point of display,
     * instead of being scattered implicitly through the templates. Nothing in
     * the UI does arithmetic on money - every figure shown is computed by the
     * database and rendered as received.
     */

    /* Parse an API money string into a Number for formatting or comparison. */
    window.num = function (value) {
        var parsed = Number(value);
        return isFinite(parsed) ? parsed : 0;
    };

    /* Fixed-decimal display, e.g. fixed(row.price, 2) -> "100.00". */
    window.fixed = function (value, places) {
        return window.num(value).toFixed(places === undefined ? 2 : places);
    };

    /* Thousands-separated USD, e.g. "$1,234.56". */
    window.usd = function (value) {
        return "$" + window.num(value).toLocaleString(undefined, {
            minimumFractionDigits: 2,
            maximumFractionDigits: 2
        });
    };
})();
