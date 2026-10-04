(function () {
  // Navigation toggle on narrow screens.
  var menu = document.querySelector('.menu');
  var nav = document.getElementById('nav');
  if (menu && nav) {
    menu.addEventListener('click', function () {
      var open = nav.classList.toggle('open');
      menu.setAttribute('aria-expanded', open ? 'true' : 'false');
    });
  }

  // Manifest callouts light up the lines they refer to.
  var lines = document.querySelectorAll('#manifest .l[data-ref]');
  var callouts = document.querySelectorAll('.callout');
  if (callouts.length) {
    var pinned = null;
    var light = function (ref) {
      lines.forEach(function (l) { l.classList.toggle('lit', !!ref && l.getAttribute('data-ref') === ref); });
      callouts.forEach(function (c) { c.classList.toggle('on', !!ref && c.getAttribute('data-for') === ref); });
    };
    callouts.forEach(function (c) {
      var ref = c.getAttribute('data-for');
      c.addEventListener('mouseenter', function () { light(ref); });
      c.addEventListener('focus', function () { light(ref); });
      c.addEventListener('mouseleave', function () { light(pinned); });
      c.addEventListener('blur', function () { light(pinned); });
      c.addEventListener('click', function () { pinned = pinned === ref ? null : ref; light(pinned); });
    });
  }

  // The approval gate. Approve runs the change through canary and promotion; reject holds the line.
  var status = document.getElementById('cr-status');
  var log = document.getElementById('cr-log');
  var approve = document.getElementById('approve');
  var reject = document.getElementById('reject');
  var reset = document.getElementById('reset');
  if (status && log && approve && reject && reset) {
    var reduce = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    var timers = [];
    var after = function (ms, fn) { if (reduce) { fn(); } else { timers.push(setTimeout(fn, ms)); } };
    var setStatus = function (tone, text) { status.className = 'st st-' + tone; status.textContent = text; };
    var entry = function (t, text) {
      var li = document.createElement('li');
      var time = document.createElement('time'); time.textContent = t;
      var span = document.createElement('span'); span.textContent = text;
      li.appendChild(time); li.appendChild(span); log.appendChild(li);
    };
    var lock = function () { approve.disabled = true; reject.disabled = true; reset.hidden = false; };
    approve.addEventListener('click', function () {
      lock();
      setStatus('ok', 'Change 9: approved');
      entry('09:02', 'Approved by you. Canary started: 5% of live calls on 0.1.2.');
      after(1400, function () {
        setStatus('warn', 'Change 9: in canary');
        entry('09:31', '200 calls, 0 errors, 0 validation failures. Verdict: pass.');
      });
      after(2900, function () {
        setStatus('ok', 'Change 9: promoted');
        entry('09:31', '0.1.2 is published. 0.1.1 superseded. Incident 14 resolved.');
      });
    });
    reject.addEventListener('click', function () {
      lock();
      setStatus('idle', 'Change 9: rejected');
      entry('09:02', 'Rejected by you. 0.1.1 stays published. Incident 14 needs a person.');
    });
    reset.addEventListener('click', function () {
      timers.forEach(clearTimeout); timers = [];
      log.textContent = '';
      setStatus('warn', 'Change 9: waiting for approval');
      approve.disabled = false; reject.disabled = false; reset.hidden = true;
    });
  }
})();
