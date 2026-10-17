<script>
  import { onMount, tick } from 'svelte';

  const storageKey = 'daylist-tasks-v1';
  const noteColors = ['yellow', 'mint', 'rose', 'peach', 'sky', 'lilac'];
  const workspaceThemes = [
    { id: 'sage', name: 'Sage', preview: '#78967b' },
    { id: 'ocean', name: 'Ocean', preview: '#6489ad' },
    { id: 'lavender', name: 'Lavender', preview: '#8a78ad' },
    { id: 'coral', name: 'Coral', preview: '#c77c6d' },
    { id: 'amber', name: 'Amber', preview: '#b08a46' },
  ];
  const themeStorageKey = 'daylist-workspace-theme-v1';
  const weekdays = ['Su', 'Mo', 'Tu', 'We', 'Th', 'Fr', 'Sa'];
  const hours = Array.from({ length: 12 }, (_, index) => String(index + 1).padStart(2, '0'));
  const minutes = Array.from({ length: 60 }, (_, index) => String(index).padStart(2, '0'));
  let tasks = $state([]);
  let filter = $state('wall');
  let searchQuery = $state('');
  let theme = $state('sage');
  let themePickerOpen = $state(false);
  let modalOpen = $state(false);
  let modalTitle = $state('');
  let modalNotes = $state('');
  let modalDue = $state('');
  let modalColor = $state('yellow');
  let datePickerOpen = $state(false);
  let calendarMonth = $state(new Date(new Date().getFullYear(), new Date().getMonth(), 1));
  let timeHour = $state('09');
  let timeMinute = $state('00');
  let timePeriod = $state('AM');
  let editingTaskId = $state(null);
  let titleInput = $state(null);
  let paperDialog = $state(null);
  let modalReturnFocus = $state(null);
  let ready = $state(false);
  let reminderPermission = $state('unsupported');
  let reminderQueue = $state([]);
  let currentTime = $state(Date.now());

  onMount(() => {
    try {
      const saved = JSON.parse(localStorage.getItem(storageKey) || '[]');
      if (Array.isArray(saved)) {
        tasks = saved.filter((task) => task && typeof task.id === 'string' && typeof task.title === 'string')
          .map((task) => ({
            id: task.id,
            title: task.title,
            done: Boolean(task.done),
            createdAt: Number(task.createdAt) || Date.now(),
            dueAt: typeof task.dueAt === 'string' && Number.isFinite(new Date(task.dueAt).getTime()) ? task.dueAt : '',
            reminded: Boolean(task.reminded),
            notes: typeof task.notes === 'string' ? task.notes : '',
            color: noteColors.includes(task.color) ? task.color : '',
          }));
      }
    } catch {
      tasks = [];
    }
    try {
      const savedTheme = localStorage.getItem(themeStorageKey);
      if (workspaceThemes.some((option) => option.id === savedTheme)) theme = savedTheme;
    } catch { /* Theme preferences may be unavailable in private browsing. */ }
    if ('Notification' in window) reminderPermission = Notification.permission;
    ready = true;
    checkReminders();
    const timer = window.setInterval(() => {
      currentTime = Date.now();
      checkReminders();
    }, 15000);
    return () => window.clearInterval(timer);
  });

  $effect(() => {
    if (ready) {
      try { localStorage.setItem(storageKey, JSON.stringify(tasks)); } catch { /* Storage may be unavailable. */ }
    }
  });

  $effect(() => {
    if (ready) {
      try { localStorage.setItem(themeStorageKey, theme); } catch { /* Theme preferences may be unavailable. */ }
    }
  });

  let selectedTheme = $derived(workspaceThemes.find((option) => option.id === theme) ?? workspaceThemes[0]);

  let todayLabel = $derived(new Intl.DateTimeFormat('en', { weekday: 'long', month: 'long', day: 'numeric' }).format(new Date()));
  let remaining = $derived(tasks.filter((task) => !task.done).length);
  let completed = $derived(tasks.filter((task) => task.done).length);
  let visibleTasks = $derived(tasks.filter((task) => {
    const matchesFilter = (filter === 'wall' && !task.done)
      || (filter === 'today' && !task.done && !isUpcoming(task) && !isMissed(task))
      || (filter === 'upcoming' && isUpcoming(task))
      || (filter === 'missed' && isMissed(task))
      || (filter === 'completed' && task.done);
    return matchesFilter && task.title.toLowerCase().includes(searchQuery.trim().toLowerCase());
  }));
  let upcomingCount = $derived(tasks.filter(isUpcoming).length);
  let todayCount = $derived(tasks.filter((task) => !task.done && !isUpcoming(task) && !isMissed(task)).length);
  let missedCount = $derived(tasks.filter(isMissed).length);
  let calendarMonthLabel = $derived(new Intl.DateTimeFormat('en', { month: 'long', year: 'numeric' }).format(calendarMonth));
  let calendarDays = $derived(makeCalendarDays(calendarMonth));

  function saveNote(event) {
    event.preventDefault();
    const title = modalTitle.trim();
    if (!title) return;
    if (editingTaskId) {
      tasks = tasks.map((task) => task.id === editingTaskId
        ? { ...task, title, notes: modalNotes, dueAt: modalDue, color: modalColor, reminded: task.dueAt === modalDue ? task.reminded : false }
        : task);
    } else {
      tasks = [{ id: crypto.randomUUID(), title, done: false, createdAt: Date.now(), dueAt: modalDue, reminded: false, notes: modalNotes, color: modalColor }, ...tasks];
    }
    filter = 'wall';
    closeEditor();
  }

  async function openCreateNote(event) {
    modalReturnFocus = event?.currentTarget;
    editingTaskId = null;
    modalTitle = '';
    modalNotes = '';
    modalDue = '';
    modalColor = 'yellow';
    datePickerOpen = false;
    calendarMonth = new Date(new Date().getFullYear(), new Date().getMonth(), 1);
    timeHour = '09';
    timeMinute = '00';
    timePeriod = 'AM';
    modalOpen = true;
    await tick();
    if (paperDialog && !paperDialog.open) paperDialog.showModal();
    titleInput?.focus();
  }

  async function openEditNote(task, event) {
    modalReturnFocus = event?.currentTarget;
    editingTaskId = task.id;
    modalTitle = task.title;
    modalNotes = task.notes;
    modalDue = task.dueAt;
    modalColor = task.color || 'yellow';
    datePickerOpen = false;
    const date = task.dueAt ? new Date(task.dueAt) : new Date();
    calendarMonth = new Date(date.getFullYear(), date.getMonth(), 1);
    const hour = date.getHours();
    timeHour = String(hour % 12 || 12).padStart(2, '0');
    timeMinute = String(date.getMinutes()).padStart(2, '0');
    timePeriod = hour >= 12 ? 'PM' : 'AM';
    modalOpen = true;
    await tick();
    if (paperDialog && !paperDialog.open) paperDialog.showModal();
    titleInput?.focus();
  }

  function closeEditor() {
    if (paperDialog?.open) paperDialog.close();
    modalOpen = false;
    datePickerOpen = false;
    editingTaskId = null;
    requestAnimationFrame(() => modalReturnFocus?.focus());
  }

  function handleWindowKeydown(event) {
    if (modalOpen && event.key === 'Escape') {
      event.preventDefault();
      closeEditor();
    } else if (reminderQueue.length && event.key === 'Escape') {
      event.preventDefault();
      dismissReminder();
    }
  }

  function noteColor(task, index) {
    return task.color || noteColors[index % noteColors.length];
  }

  function localDateString(date) {
    const year = date.getFullYear();
    const month = String(date.getMonth() + 1).padStart(2, '0');
    const day = String(date.getDate()).padStart(2, '0');
    return `${year}-${month}-${day}`;
  }

  function makeCalendarDays(month) {
    const first = new Date(month.getFullYear(), month.getMonth(), 1);
    const start = new Date(month.getFullYear(), month.getMonth(), 1 - first.getDay());
    const selectedDate = modalDue ? modalDue.split('T')[0] : '';
    const today = localDateString(new Date());
    return Array.from({ length: 42 }, (_, index) => {
      const date = new Date(start.getFullYear(), start.getMonth(), start.getDate() + index);
      const value = localDateString(date);
      return { value, day: date.getDate(), inMonth: date.getMonth() === month.getMonth(), selected: value === selectedDate, today: value === today };
    });
  }

  function setCalendarMonth(offset) {
    calendarMonth = new Date(calendarMonth.getFullYear(), calendarMonth.getMonth() + offset, 1);
  }

  function to24Hour() {
    const hour = Number(timeHour) % 12 + (timePeriod === 'PM' ? 12 : 0);
    return String(hour).padStart(2, '0');
  }

  function setPickerDate(value) {
    modalDue = `${value}T${to24Hour()}:${timeMinute}`;
    const date = new Date(`${value}T12:00`);
    calendarMonth = new Date(date.getFullYear(), date.getMonth(), 1);
  }

  function updatePickerTime(part, value) {
    if (part === 'hour') timeHour = value;
    if (part === 'minute') timeMinute = value;
    if (part === 'period') timePeriod = value;
    if (modalDue) modalDue = `${modalDue.split('T')[0]}T${to24Hour()}:${timeMinute}`;
  }

  function setQuickDate(offset) {
    const date = new Date();
    date.setDate(date.getDate() + offset);
    setPickerDate(localDateString(date));
  }

  function clearPickerDate() {
    modalDue = '';
  }

  function isUpcoming(task) {
    if (task.done || !task.dueAt) return false;
    const due = new Date(task.dueAt);
    const tomorrow = new Date();
    tomorrow.setHours(24, 0, 0, 0);
    return due.getTime() >= tomorrow.getTime();
  }

  function isMissed(task) {
    if (task.done || !task.dueAt) return false;
    const due = new Date(task.dueAt);
    const gracePeriod = 5 * 60 * 1000;
    return Number.isFinite(due.getTime()) && due.getTime() + gracePeriod <= currentTime;
  }

  function toggleTask(id) {
    tasks = tasks.map((task) => task.id === id ? { ...task, done: !task.done } : task);
  }

  function deleteTask(id) {
    tasks = tasks.filter((task) => task.id !== id);
    reminderQueue = reminderQueue.filter((task) => task.id !== id);
    if (editingTaskId === id) closeEditor();
  }

  function dismissReminder() {
    reminderQueue = reminderQueue.slice(1);
  }

  function completeReminder() {
    const current = reminderQueue[0];
    if (!current) return;
    toggleTask(current.id);
    dismissReminder();
  }

  function snoozeReminder() {
    const current = reminderQueue[0];
    if (!current) return;
    const snoozedUntil = new Date(Date.now() + 5 * 60 * 1000).toISOString();
    tasks = tasks.map((task) => task.id === current.id ? { ...task, dueAt: snoozedUntil, reminded: false } : task);
    dismissReminder();
  }

  async function enableReminders() {
    if (!('Notification' in window)) { reminderPermission = 'unsupported'; return; }
    try {
      reminderPermission = await Notification.requestPermission();
      checkReminders();
    } catch {
      reminderPermission = Notification.permission;
    }
  }

  function checkReminders() {
    if (!ready) return;
    const now = Date.now();
    const dueNow = tasks.filter((task) => !task.done && !task.reminded && task.dueAt && Number.isFinite(new Date(task.dueAt).getTime()) && new Date(task.dueAt).getTime() <= now);
    if (!dueNow.length) return;
    const ids = new Set(dueNow.map((task) => task.id));
    tasks = tasks.map((task) => ids.has(task.id) ? { ...task, reminded: true } : task);
    reminderQueue = [...reminderQueue, ...dueNow];
    if (reminderPermission === 'granted' && 'Notification' in window) {
      dueNow.forEach((task) => {
        const notification = new Notification('Task due now', { body: task.title, tag: `daylist-${task.id}` });
        notification.onclick = () => { window.focus(); notification.close(); };
      });
    }
  }

  function formatDue(dueAt) {
    const date = new Date(dueAt);
    return `${new Intl.DateTimeFormat('en', { month: 'short', day: 'numeric' }).format(date)} · ${new Intl.DateTimeFormat('en', { hour: 'numeric', minute: '2-digit' }).format(date)}`;
  }
</script>

<svelte:head>
  <title>Daylist — a little more focused</title>
</svelte:head>

<svelte:window onkeydown={handleWindowKeydown} />

<div class="page-backdrop theme-{theme}">
  <div class="app-shell">
    <aside class="sidebar">
      <a class="brand" href="#home" aria-label="Daylist home">
        <span class="brand-mark"><span></span><span></span><span></span></span>
        <span>daylist<span class="brand-period">.</span></span>
      </a>

      <div class="menu-heading"><span>MENU</span><span class="menu-mark">☰</span></div>
      <label class="search-box"><span>⌕</span><input aria-label="Search tasks" bind:value={searchQuery} placeholder="Search" /></label>

      <div class="side-label">TASKS</div>
      <nav class="nav-group" aria-label="Task views">
        <button class="nav-item" class:selected={filter === 'upcoming'} aria-current={filter === 'upcoming' ? 'page' : undefined} onclick={() => filter = 'upcoming'}>
          <span class="nav-icon">»</span><span>Upcoming</span><span class="nav-count">{upcomingCount}</span>
        </button>
        <button class="nav-item" class:selected={filter === 'today'} aria-current={filter === 'today' ? 'page' : undefined} onclick={() => filter = 'today'}>
          <span class="nav-icon">▦</span><span>Today</span><span class="nav-count">{todayCount}</span>
        </button>
        <button class="nav-item" class:selected={filter === 'missed'} aria-current={filter === 'missed' ? 'page' : undefined} onclick={() => filter = 'missed'}>
          <span class="nav-icon">!</span><span>Missed</span><span class="nav-count">{missedCount}</span>
        </button>
        <button class="nav-item" class:selected={filter === 'completed'} aria-current={filter === 'completed' ? 'page' : undefined} onclick={() => filter = 'completed'}>
          <span class="nav-icon">✓</span><span>Completed</span><span class="nav-count">{completed}</span>
        </button>
      </nav>

      <div class="side-label lists-label">VIEWS</div>
      <button class="nav-item" class:selected={filter === 'wall'} aria-current={filter === 'wall' ? 'page' : undefined} onclick={() => filter = 'wall'}>
        <span class="nav-icon">▤</span><span>To-do List</span>
      </button>

      <div class="side-label appearance-label">APPEARANCE</div>
      <button class="nav-item theme-nav-item" aria-expanded={themePickerOpen} aria-controls="workspace-theme-picker" onclick={() => themePickerOpen = !themePickerOpen}>
        <span class="nav-icon">◉</span><span>Theme</span><span class="theme-preview" style={`--swatch: ${selectedTheme.preview}`}></span><span class="theme-chevron">{themePickerOpen ? '⌃' : '⌄'}</span>
      </button>
      {#if themePickerOpen}
        <div class="theme-picker" id="workspace-theme-picker" role="group" aria-label="Choose workspace theme color">
          <span class="theme-picker-label">Workspace color</span>
          <div class="theme-swatches">
            {#each workspaceThemes as option}
              <button type="button" class="theme-swatch" class:active={theme === option.id} style={`--swatch: ${option.preview}`} aria-label={`Use ${option.name} theme`} aria-pressed={theme === option.id} title={option.name} onclick={() => theme = option.id}></button>
            {/each}
          </div>
          <span class="theme-current">{selectedTheme.name}</span>
        </div>
      {/if}

      <div class="sidebar-bottom">
        <div class="focus-card">
          <span class="focus-spark">✳</span>
          <p>Make a little progress.<br />Then make a little more.</p>
          <span class="focus-caption">A LITTLE REMINDER</span>
        </div>
      </div>
    </aside>

    <main id="home" class="main-area">
      <header class="topbar">
        <div class="breadcrumb">MY SPACE <span>/</span> <strong>{filter === 'wall' ? 'TO-DO LIST' : filter === 'today' ? 'TODAY' : filter === 'upcoming' ? 'UPCOMING' : filter === 'missed' ? 'MISSED' : 'COMPLETED'}</strong></div>
        <div class="top-actions">
          {#if reminderPermission === 'granted'}
            <span class="reminder-status enabled" title="Browser reminders are enabled">◉ <span>Reminders on</span></span>
          {:else if reminderPermission === 'denied'}
            <span class="reminder-status" title="Allow notifications for this site in your browser settings">◉ <span>Notifications blocked</span></span>
          {:else if reminderPermission === 'unsupported'}
            <span class="reminder-status" title="This browser does not support notifications">◉ <span>Reminders unavailable</span></span>
          {:else}
            <button class="reminder-button" onclick={enableReminders}>Enable reminders</button>
          {/if}
          <span class="date-chip"><span class="calendar-icon">▦</span>{todayLabel}</span>
        </div>
      </header>

      <section class="content">
        <div class="page-heading">
          <div><div class="eyebrow"><span class="sun-icon">✳</span> YOUR TASKS, ALL IN ONE PLACE</div><h1>{filter === 'upcoming' ? 'Upcoming' : filter === 'today' ? 'Today' : filter === 'missed' ? 'Missed' : filter === 'completed' ? 'Completed' : 'To-do List'} <span class="heading-count">{visibleTasks.length}</span></h1></div>
          <div class="heading-date">{todayLabel}</div>
        </div>

        <div class="list-heading"><div><h2>{filter === 'wall' ? 'Your tasks' : filter === 'upcoming' ? 'Coming up' : filter === 'today' ? 'For today' : filter === 'missed' ? 'Missed tasks' : 'Completed tasks'}</h2><span class="task-total">{remaining} {remaining === 1 ? 'task' : 'tasks'} to go</span></div>
          <div class="filters" aria-label="Filter tasks">
            <button class:active={filter === 'wall'} aria-pressed={filter === 'wall'} onclick={() => filter = 'wall'}>Open <span>{remaining}</span></button>
            <button class:active={filter === 'today'} aria-pressed={filter === 'today'} onclick={() => filter = 'today'}>To do <span>{remaining}</span></button>
            <button class:active={filter === 'completed'} aria-pressed={filter === 'completed'} onclick={() => filter = 'completed'}>Done <span>{completed}</span></button>
          </div>
        </div>

        <div class="sticky-grid">
          {#if visibleTasks.length}
            {#each visibleTasks as task, index (task.id)}
              <article class="sticky-note" class:note-yellow={noteColor(task, index) === 'yellow'} class:note-mint={noteColor(task, index) === 'mint'} class:note-rose={noteColor(task, index) === 'rose'} class:note-peach={noteColor(task, index) === 'peach'} class:note-sky={noteColor(task, index) === 'sky'} class:note-lilac={noteColor(task, index) === 'lilac'} class:done-note={task.done}>
                <div class="note-topline">
                  <button class="check-button" class:checked={task.done} aria-label={task.done ? `Mark ${task.title} as to do` : `Complete ${task.title}`} onclick={() => toggleTask(task.id)}>{#if task.done}<span>✓</span>{/if}</button>
                  <button class="note-edit" aria-label={`Edit ${task.title}`} title="Edit task" onclick={(event) => openEditNote(task, event)}>✎</button>
                  <button class="delete-button" aria-label={`Delete ${task.title}`} onclick={() => deleteTask(task.id)}>×</button>
                </div>
                <button class="note-title" class:crossed-out={task.done} onclick={(event) => openEditNote(task, event)}>{task.title}</button>
                <p class="note-body">{task.notes || 'Add a few details to this task...'}</p>
                <div class="note-due">◷ <span>{task.dueAt ? formatDue(task.dueAt) : 'Add due date & time'}</span></div>
              </article>
            {/each}
          {:else if searchQuery.trim()}
            <div class="empty-state"><div class="empty-icon">⌕</div><strong>No matching tasks.</strong><span>Try another search.</span></div>
          {:else}
            <div class="empty-state"><div class="empty-icon">{filter === 'wall' ? '✓' : filter === 'missed' ? '!' : '✳'}</div><strong>{filter === 'wall' ? 'You’re all caught up.' : filter === 'upcoming' ? 'Nothing coming up yet.' : filter === 'missed' ? 'No missed tasks.' : filter === 'completed' ? 'No completed tasks yet.' : 'Nothing planned for today.'}</strong><span>{filter === 'wall' ? 'Completed tasks are in Completed.' : filter === 'upcoming' ? 'Add a future due date to a task.' : filter === 'missed' ? 'Tasks past their due date will show here.' : filter === 'completed' ? 'Finished tasks will collect here.' : 'Add a task or choose another view.'}</span></div>
          {/if}
          <button class="add-note-tile" type="button" onclick={openCreateNote} aria-label="Add a new task">
            <div class="new-note-icon">+</div>
            <span class="add-note-label">Add a new task...</span>
          </button>
        </div>
        <footer class="content-footer"><span>MADE FOR A CALMER KIND OF PRODUCTIVITY</span><span>ONE THING AT A TIME <b>✳</b></span></footer>
      </section>
    </main>

  </div>
  {#if modalOpen}
    <div class="modal-backdrop">
      <dialog bind:this={paperDialog} class="paper-sheet paper-{modalColor}" aria-labelledby="paper-title" oncancel={(event) => { event.preventDefault(); closeEditor(); }}>
        <div class="paper-header"><span class="paper-kicker">DAYLIST <span>·</span> YOUR TASKS</span><button type="button" class="paper-close" aria-label="Close editor" onclick={closeEditor}>×</button></div>
        <h2 id="paper-title">{editingTaskId ? 'Edit your task' : 'A new task'}</h2>
        <form class="paper-form" onsubmit={saveNote}>
          <label class="paper-label" for="paper-task-title">WHAT DO YOU NEED TO DO?</label>
          <input id="paper-task-title" class="paper-title-input" bind:this={titleInput} bind:value={modalTitle} maxlength="160" placeholder="Write a task title..." required />

          <label class="paper-label" for="paper-task-notes">TASK DETAILS</label>
          <textarea id="paper-task-notes" class="paper-notes-input" bind:value={modalNotes} rows="5" placeholder="Add any notes or details here..."></textarea>

          <div class="paper-field-heading"><span class="paper-label">DATE & TIME</span><span class="optional-label">OPTIONAL</span></div>
          <div class="date-time-control">
            <button class="date-trigger" type="button" aria-expanded={datePickerOpen} onclick={() => datePickerOpen = !datePickerOpen}>
              <span class="date-trigger-icon">▦</span>
              <span class:date-placeholder={!modalDue}>{modalDue ? formatDue(modalDue) : 'Choose a date and time'}</span>
              <span class="date-trigger-chevron">{datePickerOpen ? '⌃' : '⌄'}</span>
            </button>
            {#if datePickerOpen}
              <div class="calendar-popover" aria-label="Choose due date and time">
                <div class="calendar-header">
                  <button type="button" aria-label="Previous month" onclick={() => setCalendarMonth(-1)}>‹</button>
                  <strong>{calendarMonthLabel}</strong>
                  <button type="button" aria-label="Next month" onclick={() => setCalendarMonth(1)}>›</button>
                </div>
                <div class="picker-layout">
                  <div class="calendar-section">
                    <div class="weekdays">{#each weekdays as day}<span>{day}</span>{/each}</div>
                    <div class="calendar-days">
                      {#each calendarDays as day (day.value)}
                        <button type="button" class:outside-month={!day.inMonth} class:today={day.today} class:selected-day={day.selected} aria-label={new Intl.DateTimeFormat('en', { dateStyle: 'full' }).format(new Date(`${day.value}T12:00`))} aria-pressed={day.selected} onclick={() => setPickerDate(day.value)}>{day.day}</button>
                      {/each}
                    </div>
                    <div class="calendar-quick-actions"><button type="button" onclick={() => setQuickDate(0)}>Today</button><button type="button" onclick={() => setQuickDate(1)}>Tomorrow</button><button type="button" onclick={clearPickerDate}>Clear</button></div>
                  </div>
                  <div class="time-section">
                    <span class="picker-section-label">TIME</span>
                    <label><span>Hour</span><select aria-label="Hour" value={timeHour} onchange={(event) => updatePickerTime('hour', event.currentTarget.value)}>{#each hours as hour}<option value={hour}>{hour}</option>{/each}</select></label>
                    <label><span>Minute</span><select aria-label="Minute" value={timeMinute} onchange={(event) => updatePickerTime('minute', event.currentTarget.value)}>{#each minutes as minute}<option value={minute}>{minute}</option>{/each}</select></label>
                    <label><span>Period</span><select aria-label="AM or PM" value={timePeriod} onchange={(event) => updatePickerTime('period', event.currentTarget.value)}><option value="AM">AM</option><option value="PM">PM</option></select></label>
                  </div>
                </div>
              </div>
            {/if}
          </div>

          <fieldset class="color-fieldset">
            <legend class="paper-label">PAPER COLOR</legend>
            <div class="color-options">
              {#each noteColors as color}
                <button type="button" class="color-swatch swatch-{color}" class:chosen={modalColor === color} aria-label={`Choose ${color} paper`} aria-pressed={modalColor === color} onclick={() => modalColor = color}></button>
              {/each}
            </div>
          </fieldset>

          <div class="paper-actions"><button type="button" class="cancel-note" onclick={closeEditor}>Cancel</button><button class="save-note" type="submit">{editingTaskId ? 'Save changes' : 'Add task'} <span>↗</span></button></div>
        </form>
      </dialog>
    </div>
  {/if}
  {#if reminderQueue.length}
    {@const reminder = reminderQueue[0]}
    <div class="due-reminder-overlay">
      <dialog open class="due-reminder-card" aria-modal="true" aria-labelledby="due-reminder-title" aria-describedby="due-reminder-details">
        <div class="reminder-card-top">
          <span class="reminder-brand"><span class="reminder-brand-mark">d.</span> DAYLIST REMINDER</span>
          <button class="reminder-close" type="button" aria-label="Dismiss reminder" onclick={dismissReminder}>×</button>
        </div>
        <div class="reminder-clock-mark" aria-hidden="true"><span>◷</span></div>
        <span class="due-now-label"><span></span> DUE NOW</span>
        <h2 id="due-reminder-title">Time for this task</h2>
        <p class="reminder-intro">You planned a moment for:</p>
        <div class="reminder-task-panel">
          <strong>{reminder.title}</strong>
          {#if reminder.notes}<p id="due-reminder-details">{reminder.notes}</p>{:else}<p id="due-reminder-details">Take a breath and make a start when you're ready.</p>{/if}
          {#if reminder.dueAt}<span class="reminder-due-time">SCHEDULED FOR · {formatDue(reminder.dueAt)}</span>{/if}
        </div>
        <div class="reminder-actions">
          <button class="reminder-complete" type="button" onclick={completeReminder}><span>✓</span> Mark as complete</button>
          <button class="reminder-snooze" type="button" onclick={snoozeReminder}>Remind me in 5 minutes</button>
        </div>
        <button class="reminder-dismiss" type="button" onclick={dismissReminder}>Dismiss reminder</button>
        {#if reminderQueue.length > 1}<span class="reminder-queue-count">{reminderQueue.length - 1} more due {reminderQueue.length === 2 ? 'task' : 'tasks'}</span>{/if}
      </dialog>
    </div>
  {/if}
</div>
