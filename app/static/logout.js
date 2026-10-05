// HTTP Basic Auth has no server session, so "logout" means overwriting the
// browser's cached credentials with dummy ones, then showing a logged-out page.
function dbiLogout(ev) {
  if (ev) ev.preventDefault();
  var done = function () { window.location.href = "/logout"; };
  try {
    var xhr = new XMLHttpRequest();
    xhr.open("GET", "/logout?clear=1", true, "logged-out", "x");
    xhr.onloadend = done;
    xhr.send();
  } catch (e) { done(); }
  return false;
}
document.addEventListener("click", function (ev) {
  var t = ev.target.closest ? ev.target.closest("[data-logout]") : null;
  if (t) dbiLogout(ev);
});
