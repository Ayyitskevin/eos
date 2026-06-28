(function () {
  if (!document.querySelector('.calendar-page')) return;

  var dragged = null;

  document.querySelectorAll('.draggable-event').forEach(function (el) {
    el.addEventListener('dragstart', function (e) {
      dragged = el;
      e.dataTransfer.setData('text/plain', el.dataset.apptId || '');
      e.dataTransfer.effectAllowed = 'move';
    });
  });

  function dropTarget(cell) {
    if (!cell || !dragged) return;
    var apptId = dragged.dataset.apptId;
    var startsAt = dragged.dataset.startsAt || '';
    var date = cell.dataset.date;
    if (!apptId || !date || !startsAt) return;
    var timePart = startsAt.length >= 19 ? startsAt.slice(11, 19) : '09:00:00';
    var form = document.getElementById('reschedule-form');
    if (!form) return;
    form.action = '/admin/appointments/' + apptId + '/reschedule';
    document.getElementById('reschedule-starts').value = date + 'T' + timePart.slice(0, 5);
    form.submit();
  }

  document.querySelectorAll('.cal-day[data-date], .cal-week-col[data-date]').forEach(function (cell) {
    cell.addEventListener('dragover', function (e) {
      e.preventDefault();
      cell.classList.add('cal-drop-target');
    });
    cell.addEventListener('dragleave', function () {
      cell.classList.remove('cal-drop-target');
    });
    cell.addEventListener('drop', function (e) {
      e.preventDefault();
      cell.classList.remove('cal-drop-target');
      dropTarget(cell);
    });
  });
})();