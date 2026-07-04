// Global App State
let currentTab = 'dashboard';
let taskPollInterval = null;
let selectedFile = null;

// DOM Elements
const tabDashboardBtn = document.getElementById('tab-dashboard');
const tabSettingsBtn = document.getElementById('tab-settings');
const viewDashboard = document.getElementById('view-dashboard');
const viewSettings = document.getElementById('view-settings');
const pageTitle = document.getElementById('page-title');
const pageSubtitle = document.getElementById('page-subtitle');
const tgConnectionStatus = document.getElementById('tg-connection-status');

// Forms & Inputs
const downloadForm = document.getElementById('download-form');
const magnetInput = document.getElementById('magnet-input');
const torrentFileInput = document.getElementById('torrent-file');
const fileDropzone = document.getElementById('file-dropzone');
const selectedFileNameDisp = document.getElementById('selected-file-name');
const btnSubmit = document.getElementById('btn-submit');
const btnSpinner = document.getElementById('btn-spinner');
const btnText = document.getElementById('btn-text');

const settingsForm = document.getElementById('settings-form');
const apiIdInput = document.getElementById('api-id');
const apiHashInput = document.getElementById('api-hash');
const botTokenInput = document.getElementById('bot-token');
const channelUsernameInput = document.getElementById('channel-username');

// List Containers
const tasksListContainer = document.getElementById('tasks-list');
const taskCountLabel = document.getElementById('task-count');
const toastMessage = document.getElementById('toast-message');

// Initialize App
document.addEventListener('DOMContentLoaded', () => {
    switchTab('dashboard');
    loadConfig();
    startPolling();
    setupEventListeners();
});

// Event Listeners
function setupEventListeners() {
    // Navigation
    tabDashboardBtn.addEventListener('click', () => switchTab('dashboard'));
    tabSettingsBtn.addEventListener('click', () => switchTab('settings'));

    // Drag & Drop Handlers
    fileDropzone.addEventListener('click', () => torrentFileInput.click());
    
    torrentFileInput.addEventListener('change', (e) => {
        if (e.target.files.length > 0) {
            handleSelectedFile(e.target.files[0]);
        }
    });

    fileDropzone.addEventListener('dragover', (e) => {
        e.preventDefault();
        fileDropzone.classList.add('dragover');
    });

    fileDropzone.addEventListener('dragleave', () => {
        fileDropzone.classList.remove('dragover');
    });

    fileDropzone.addEventListener('drop', (e) => {
        e.preventDefault();
        fileDropzone.classList.remove('dragover');
        if (e.dataTransfer.files.length > 0) {
            handleSelectedFile(e.dataTransfer.files[0]);
        }
    });

    // Form Submissions
    downloadForm.addEventListener('submit', handleDownloadSubmit);
    settingsForm.addEventListener('submit', handleSettingsSubmit);
}

// Tab Switching
function switchTab(tab) {
    currentTab = tab;
    if (tab === 'dashboard') {
        tabDashboardBtn.classList.add('active');
        tabSettingsBtn.classList.remove('active');
        viewDashboard.style.display = 'flex';
        viewSettings.style.display = 'block'; // Or block
        viewSettings.style.display = 'none';
        pageTitle.innerText = 'Yuklash Paneli';
        pageSubtitle.innerText = 'Torrent va Magnet havolalarni yuklab olib, Telegram kanalga yuboring';
    } else {
        tabDashboardBtn.classList.remove('active');
        tabSettingsBtn.classList.add('active');
        viewDashboard.style.display = 'none';
        viewSettings.style.display = 'block';
        pageTitle.innerText = 'Dastur Sozlamalari';
        pageSubtitle.innerText = 'Telegram API va Bot integratsiyasini sozlash';
    }
}

// Toast Notifications
function showToast(message, isError = false) {
    toastMessage.innerText = message;
    toastMessage.style.borderLeftColor = isError ? 'var(--color-error)' : 'var(--color-primary)';
    toastMessage.classList.add('show');
    
    setTimeout(() => {
        toastMessage.classList.remove('show');
    }, 4000);
}

// File Upload Handler
function handleSelectedFile(file) {
    if (!file.name.endsWith('.torrent')) {
        showToast('Iltimos, faqat .torrent formatidagi faylni tanlang!', true);
        return;
    }
    selectedFile = file;
    selectedFileNameDisp.innerText = `Tanlangan: ${file.name}`;
    magnetInput.value = ''; // Clear magnet input if file is chosen
}

// Load Configuration from API
async function loadConfig() {
    try {
        const response = await fetch('/api/config');
        const data = await response.json();
        
        if (data.is_configured) {
            tgConnectionStatus.innerText = 'Ulangan';
            tgConnectionStatus.className = 'status-badge success';
            
            // Prefill inputs
            apiIdInput.value = data.api_id;
            apiHashInput.value = data.api_hash;
            botTokenInput.placeholder = 'Saqlangan (Tahrirlash uchun yozing)';
            channelUsernameInput.value = data.channel_username;
        } else {
            tgConnectionStatus.innerText = 'Sozlanmagan';
            tgConnectionStatus.className = 'status-badge error';
        }
    } catch (error) {
        console.error('Error loading config:', error);
    }
}

// Save Configuration
async function handleSettingsSubmit(e) {
    e.preventDefault();
    
    const apiId = apiIdInput.value.trim();
    const apiHash = apiHashInput.value.trim();
    const botToken = botTokenInput.value.trim() || botTokenInput.placeholder;
    const channelUsername = channelUsernameInput.value.trim();

    if (!apiId || !apiHash || botToken.startsWith('Saqlangan')) {
        if (botToken.startsWith('Saqlangan')) {
            showToast("Iltimos, yangi sozlamalar uchun bot tokenni to'liq kiriting.", true);
            return;
        }
    }

    try {
        const response = await fetch('/api/config', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                api_id: apiId,
                api_hash: apiHash,
                bot_token: botToken,
                channel_username: channelUsername
            })
        });
        
        const data = await response.json();
        if (response.ok) {
            showToast('Sozlamalar muvaffaqiyatli saqlandi va Telegram ulandi!');
            loadConfig();
            switchTab('dashboard');
        } else {
            showToast(data.detail || 'Sozlamalarni saqlashda xatolik yuz berdi.', true);
        }
    } catch (error) {
        showToast('Server bilan bog\'lanishda xato.', true);
    }
}

// Submit Download Task
async function handleDownloadSubmit(e) {
    e.preventDefault();
    
    const magnet = magnetInput.value.trim();
    
    if (!magnet && !selectedFile) {
        showToast('Iltimos, magnet havola kiriting yoki torrent faylini yuklang!', true);
        return;
    }

    // Show loading
    btnSubmit.disabled = true;
    btnText.style.display = 'none';
    btnSpinner.style.display = 'block';

    const formData = new FormData();
    if (magnet) formData.append('magnet', magnet);
    if (selectedFile) formData.append('file', selectedFile);

    try {
        const response = await fetch('/api/download', {
            method: 'POST',
            body: formData
        });
        
        const data = await response.json();
        
        if (response.ok) {
            showToast('Yuklash vazifasi muvaffaqiyatli qo\'shildi!');
            downloadForm.reset();
            selectedFile = null;
            selectedFileNameDisp.innerText = '';
            refreshTasks();
        } else {
            showToast(data.detail || 'Vazifani qo\'shib bo\'lmadi.', true);
        }
    } catch (error) {
        showToast('Serverga ulanishda xatolik.', true);
    } finally {
        btnSubmit.disabled = false;
        btnText.style.display = 'block';
        btnSpinner.style.display = 'none';
    }
}

// Polling for Task Status
function startPolling() {
    refreshTasks();
    taskPollInterval = setInterval(refreshTasks, 1500);
}

// Fetch Tasks and Render UI
async function refreshTasks() {
    try {
        const response = await fetch('/api/tasks');
        const tasks = await response.json();
        
        taskCountLabel.innerText = `${tasks.length} vazifa`;
        
        if (tasks.length === 0) {
            tasksListContainer.innerHTML = `
                <div class="no-tasks-state">
                    <svg class="no-tasks-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">
                        <circle cx="12" cy="12" r="10"/>
                        <path d="M8 12h8M12 8v8"/>
                    </svg>
                    <p>Hozircha vazifalar yo'q. Magnet havola yoki torrent yuboring.</p>
                </div>
            `;
            return;
        }

        let tasksHTML = '';
        
        tasks.forEach(task => {
            let statusText = '';
            let statusClass = task.status;
            
            // Translate status and pick labels
            switch (task.status) {
                case 'pending':
                    statusText = 'Kutilmoqda';
                    break;
                case 'metadata':
                    statusText = 'Metadata olinmoqda';
                    break;
                case 'downloading':
                    statusText = 'Yuklanmoqda';
                    break;
                case 'processing':
                    statusText = 'Ishlov berilmoqda';
                    break;
                case 'uploading':
                    statusText = 'Telegramga yuklanmoqda';
                    break;
                case 'completed':
                    statusText = 'Bajarildi';
                    break;
                case 'failed':
                    statusText = 'Xatolik';
                    break;
                case 'warning':
                    statusText = 'Ogohlantirish';
                    break;
                default:
                    statusText = task.status;
            }

            // Build Stats display
            let statsHTML = '';
            if (task.status === 'downloading' || task.status === 'metadata') {
                statsHTML = `
                    <div class="stat-item">
                        <svg class="stat-icon" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path d="M12 5v14M19 12l-7 7-7-7"/></svg>
                        <span>${task.speed}</span>
                    </div>
                    <div class="stat-item">
                        <span>Hajm: ${task.downloaded} / ${task.total}</span>
                    </div>
                    <div class="stat-item">
                        <svg class="stat-icon" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><circle cx="12" cy="12" r="10"/><path d="M12 6v6l4 2"/></svg>
                        <span>ETA: ${task.eta}</span>
                    </div>
                `;
            } else if (task.status === 'uploading') {
                statsHTML = `
                    <div class="stat-item">
                        <svg class="stat-icon" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path d="M12 19V5M5 12l7-7 7 7"/></svg>
                        <span>${task.speed}</span>
                    </div>
                    <div class="stat-item">
                        <span>Yuklandi: ${task.downloaded} / ${task.total}</span>
                    </div>
                `;
            } else if (task.status === 'completed') {
                statsHTML = `<span>Fayl muvaffaqiyatli Telegram kanalga yuborildi.</span>`;
            }

            tasksHTML += `
                <div class="task-item" id="${task.id}">
                    <div class="task-top">
                        <span class="task-title" title="${task.name}">${task.name}</span>
                        <span class="task-badge ${statusClass}">${statusText}</span>
                    </div>
                    
                    ${task.status === 'uploading' && task.current_file ? `
                        <div class="task-current-file">Hozirgi: ${task.current_file}</div>
                    ` : ''}
                    
                    ${['downloading', 'uploading', 'metadata'].includes(task.status) ? `
                        <div class="progress-container">
                            <div class="progress-track">
                                <div class="progress-fill" style="width: ${task.percent}%"></div>
                            </div>
                        </div>
                    ` : ''}
                    
                    <div class="task-stats">
                        ${statsHTML}
                    </div>
                    
                    ${task.error ? `<div class="task-error">${task.error}</div>` : ''}
                </div>
            `;
        });
        
        tasksListContainer.innerHTML = tasksHTML;
        
    } catch (error) {
        console.error('Error refreshing tasks:', error);
    }
}
