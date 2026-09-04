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
})();
