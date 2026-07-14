document.getElementById('loginForm').addEventListener('submit', async (e) => {
    e.preventDefault();
    const password = document.getElementById('password').value;
    const errorMsg = document.getElementById('errorMsg');
    errorMsg.textContent = '';

    try {
        const resp = await fetch('/api/login', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ password })
        });
        const data = await resp.json();

        if (data.success) {
            window.location.href = '/';
        } else {
            errorMsg.textContent = data.msg;
        }
    } catch (err) {
        errorMsg.textContent = '网络错误，请重试';
    }
});

// 加载服务器信息
fetch('/api/public-info')
    .then(r => r.json())
    .then(data => {
        if (data.success && data.public_ip) {
            document.getElementById('serverInfo').textContent =
                `服务器: ${data.public_ip}:${data.port}`;
        }
    })
    .catch(() => {});