import os
import sys
import socket
import uuid
import re
import json
import sqlite3
import secrets
import threading
import logging
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from flask import Flask, jsonify, request, send_file, session
from PIL import Image
import fitz

logging.basicConfig(level=logging.INFO, format='[%(asctime)s] %(message)s', datefmt='%H:%M:%S')
log = logging.getLogger(__name__)

try:
    import pytesseract
    pytesseract.pytesseract.tesseract_cmd = r'C:\Program Files\Tesseract-OCR\tesseract.exe'
    os.environ['TESSDATA_PREFIX'] = os.path.join(os.environ['LOCALAPPDATA'], 'Tesseract-OCR', 'tessdata')
    TESSERACT_DISPONIVEL = True
except Exception as e:
    log.warning('Tesseract OCR nao disponivel: %s', e)
    TESSERACT_DISPONIVEL = False

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TEMPLATES_DIR = os.path.join(BASE_DIR, 'templates')
COMPROVANTES_DIR = os.path.join(BASE_DIR, 'comprovantes')
DATABASE = os.path.join(BASE_DIR, 'database.db')
BACKUP_DIR = os.path.join(BASE_DIR, 'backups')
LOG_DIR = os.path.join(BASE_DIR, 'logs')
AUDIT_LOG = os.path.join(LOG_DIR, 'auditoria.jsonl')

app = Flask(__name__)
SECRET_KEY_FILE = os.path.join(BASE_DIR, 'secret_key.txt')
def get_secret_key():
    if os.path.exists(SECRET_KEY_FILE):
        with open(SECRET_KEY_FILE, 'r') as f:
            key = f.read().strip()
            if key:
                return key
    key = secrets.token_urlsafe(32)
    with open(SECRET_KEY_FILE, 'w') as f:
        f.write(key)
    return key
app.secret_key = get_secret_key()
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',
    SESSION_COOKIE_SECURE=False,  # True em producao (HTTPS)
    PERMANENT_SESSION_LIFETIME=3600,
    SESSION_REFRESH_EACH_REQUEST=False,
)
os.makedirs(COMPROVANTES_DIR, exist_ok=True)

API_TOKEN_FILE = os.path.join(BASE_DIR, 'api_token.txt')

def get_api_token():
    if os.path.exists(API_TOKEN_FILE):
        with open(API_TOKEN_FILE, 'r') as f:
            token = f.read().strip()
            if token:
                return token
    token = secrets.token_urlsafe(32)
    with open(API_TOKEN_FILE, 'w') as f:
        f.write(token)
    return token

API_TOKEN = get_api_token()
AUTO_CADASTRO_TOKEN = secrets.token_urlsafe(16)

PRECO_ATE_50 = 7
PRECO_ACIMA_50 = 10

os.makedirs(BACKUP_DIR, exist_ok=True)
os.makedirs(LOG_DIR, exist_ok=True)
AUDIT_LOG_LOCK = threading.Lock()


def audit_log(acao, detalhe=''):
    try:
        ip = request.remote_addr or 'desconhecido'
        role = session.get('role', 'token')
        entry = {
            'ts': time.strftime('%Y-%m-%dT%H:%M:%S'),
            'ip': ip,
            'role': role,
            'acao': acao,
            'detalhe': detalhe
        }
        with AUDIT_LOG_LOCK:
            with open(AUDIT_LOG, 'a', encoding='utf-8') as f:
                f.write(json.dumps(entry, ensure_ascii=False) + '\n')
    except Exception as e:
        log.warning('Erro ao escrever log de auditoria: %s', e)


def backup_db():
    try:
        ts = time.strftime('%Y-%m-%d_%H-%M-%S')
        path = os.path.join(BACKUP_DIR, f'db-{ts}.db')
        conn = get_db()
        conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        conn.close()
        with open(DATABASE, 'rb') as src:
            with open(path, 'wb') as dst:
                dst.write(src.read())
        log.info('Backup criado: %s', path)
        keep = 50
        backups = sorted(
            [os.path.join(BACKUP_DIR, f) for f in os.listdir(BACKUP_DIR) if f.startswith('db-')],
            key=os.path.getmtime, reverse=True
        )
        for old in backups[keep:]:
            try:
                os.remove(old)
            except OSError:
                pass
    except Exception as e:
        log.warning('Erro ao fazer backup: %s', e)


ocr_executor = ThreadPoolExecutor(max_workers=4)
ocr_results = {}
ocr_results_lock = threading.Lock()


def get_db():
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA busy_timeout=5000')
    return conn


def init_db():
    conn = get_db()
    conn.executescript('''
        CREATE TABLE IF NOT EXISTS registros (
            id TEXT NOT NULL,
            uuid TEXT NOT NULL DEFAULT '',
            nome TEXT NOT NULL DEFAULT '',
            telefone TEXT NOT NULL DEFAULT '',
            email TEXT NOT NULL DEFAULT '',
            quantidade_pulseiras TEXT NOT NULL DEFAULT '',
            pagamento TEXT NOT NULL DEFAULT 'Pendente',
            numeros_sorte TEXT NOT NULL DEFAULT '',
            comprovante TEXT NOT NULL DEFAULT '',
            comprovante_nome TEXT NOT NULL DEFAULT '',
            comprovante_analise TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS excluidos (
            id TEXT NOT NULL PRIMARY KEY
        );
        CREATE INDEX IF NOT EXISTS idx_registros_id ON registros(id);
    ''')
    conn.commit()
    conn.close()


@app.after_request
def add_cors_headers(response):
    origin = request.headers.get('Origin', '')
    if origin:
        response.headers['Access-Control-Allow-Origin'] = origin
        response.headers['Access-Control-Allow-Credentials'] = 'true'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type, Authorization'
    response.headers['Access-Control-Allow-Methods'] = 'GET, POST, PUT, DELETE, OPTIONS'
    response.headers['Access-Control-Max-Age'] = '86400'
    return response


@app.before_request
def handle_options():
    if request.method == 'OPTIONS':
        return jsonify({}), 200


@app.before_request
def log_requests():
    if request.path.startswith('/api/') and request.method in ('POST', 'GET') and request.path not in ('/dados',):
        log.info('%s %s (auth=%s..., content_type=%s)',
                 request.method, request.path,
                 (request.headers.get('Authorization', '')[:30] + '...') if request.headers.get('Authorization') else 'none',
                 request.content_type or 'N/A')


app.config['MAX_CONTENT_LENGTH'] = 10 * 1024 * 1024

ALLOWED_EXTENSIONS = {'pdf', 'png', 'jpg', 'jpeg', 'gif', 'webp'}

PIX_CORRETO = 'luanborges26@outlook.com'
DOMAIN_PUBLICO = 'https://meu-isaque-e-minha-rebeca.onrender.com'


_request_times = defaultdict(list)

def check_rate_limit(max_requests=120, window_seconds=60):
    ip = request.remote_addr or 'unknown'
    now = time.time()
    cutoff = now - window_seconds
    _request_times[ip] = [t for t in _request_times[ip] if t > cutoff]
    if len(_request_times[ip]) >= max_requests:
        return False
    _request_times[ip].append(now)
    return True



def get_current_role(default_to=''):
    auth = request.headers.get('Authorization', '')
    token = auth[7:] if auth.startswith('Bearer ') else ''
    if not token:
        token = request.form.get('_token', '')
    if token:
        page_tokens = session.get('page_tokens', {})
        if token in page_tokens:
            return page_tokens[token]
        if token == API_TOKEN:
            return 'admin'
        if token == AUTO_CADASTRO_TOKEN:
            return 'auto_cadastro'
        log.warning('Token nao reconhecido (page_tokens=%s)', page_tokens)
    role = session.get('role')
    if role:
        return role
    return default_to if default_to else None


def require_role(admin_only=False):
    if not check_rate_limit():
        return False

    role = get_current_role()
    if role:
        if admin_only:
            return role == 'admin'
        return True

    return False


def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


def extrair_dados_texto(texto):
    resultado = {
        'valor_encontrado': None,
        'chave_pix_encontrada': None,
        'data_pagamento': None,
        'hora_pagamento': None
    }

    valores = re.findall(r'(?:R\$\s*)?(\d{1,3}(?:\.\d{3})*(?:,\d{2})|\d+(?:,\d{2}))', texto)
    if valores:
        v = valores[0].replace('.', '').replace(',', '.')
        try:
            resultado['valor_encontrado'] = round(float(v), 2)
        except:
            pass

    chaves = re.findall(r'[\w.+-]+@[\w-]+\.[\w.-]+', texto)
    if chaves:
        resultado['chave_pix_encontrada'] = chaves[0]

    datas = re.findall(r'\b(\d{2}/\d{2}/\d{4})\b', texto)
    if datas:
        resultado['data_pagamento'] = datas[0]

    horas = re.findall(r'\b(\d{2}:\d{2}(?::\d{2})?)\b', texto)
    if horas:
        resultado['hora_pagamento'] = horas[0]

    return resultado


def analisar_imagem(filepath):
    try:
        if not TESSERACT_DISPONIVEL:
            return extrair_dados_texto(''), ''
        img = Image.open(filepath)
        texto = pytesseract.image_to_string(img, lang='por')
        return extrair_dados_texto(texto), texto
    except Exception as e:
        print('Erro OCR imagem:', e)
        return extrair_dados_texto(''), ''


def analisar_pdf(filepath):
    texto = ''
    try:
        doc = fitz.open(filepath)
        for page_num in range(len(doc)):
            page = doc[page_num]
            texto_pagina = page.get_text()
            texto += texto_pagina + '\n'

            if TESSERACT_DISPONIVEL:
                pix = page.get_pixmap(dpi=200)
                img_bytes = pix.tobytes('png')
                img_path = filepath.replace('.pdf', f'_page{page_num}.png')
                with open(img_path, 'wb') as f:
                    f.write(img_bytes)
                try:
                    img = Image.open(img_path)
                    texto_ocr = pytesseract.image_to_string(img, lang='por')
                    texto += texto_ocr + '\n'
                except Exception as e:
                    print('Erro OCR pagina PDF:', e)
                finally:
                    if os.path.exists(img_path):
                        os.remove(img_path)
        doc.close()
    except Exception as e:
        print('Erro ao abrir PDF:', e)
        return extrair_dados_texto(''), ''

    return extrair_dados_texto(texto), texto


def ler_registros():
    try:
        conn = get_db()
        rows = conn.execute('SELECT * FROM registros').fetchall()
        conn.close()
        result = []
        for row in rows:
            d = dict(row)
            d['_uuid'] = d.pop('uuid', '')
            result.append(d)
        return result
    except Exception as e:
        print('Erro ler registros:', e)
        return []


def ler_excluidos():
    try:
        conn = get_db()
        rows = conn.execute('SELECT id FROM excluidos').fetchall()
        conn.close()
        return [row['id'] for row in rows]
    except Exception as e:
        print('Erro ler excluidos:', e)
        return []


def salvar_tudo(registros_data, excluidos_data):
    conn = get_db()
    try:
        conn.execute('DELETE FROM registros')

        rows = [
            (
                str(item.get('id', '')),
                str(item.get('_uuid', '')),
                str(item.get('nome', '')),
                str(item.get('telefone', '')),
                str(item.get('email', '')),
                str(item.get('quantidade_pulseiras', '')),
                str(item.get('pagamento', 'Pendente')),
                str(item.get('numeros_sorte', '')),
                str(item.get('comprovante', '')),
                str(item.get('comprovante_nome', '')),
                str(item.get('comprovante_analise', ''))
            )
            for item in registros_data
        ]
        if rows:
            conn.executemany(
                'INSERT INTO registros (id, uuid, nome, telefone, email, quantidade_pulseiras, pagamento, numeros_sorte, comprovante, comprovante_nome, comprovante_analise) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                rows
            )

        excs_existing = set(str(r['id']) for r in conn.execute('SELECT id FROM excluidos').fetchall())
        excs_incoming = set(str(x) for x in excluidos_data)
        merged = excs_existing | excs_incoming

        conn.execute('DELETE FROM excluidos')
        if merged:
            conn.executemany('INSERT INTO excluidos (id) VALUES (?)', [(eid,) for eid in merged])

        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    try:
        comprovantes_ativos = set()
        for item in registros_data:
            c = str(item.get('comprovante', ''))
            if c:
                comprovantes_ativos.add(c)
        for fname in os.listdir(COMPROVANTES_DIR):
            if fname not in comprovantes_ativos:
                try:
                    os.remove(os.path.join(COMPROVANTES_DIR, fname))
                except OSError as e:
                    log.warning('Erro ao deletar comprovante %s: %s', fname, e)
    except OSError as e:
        log.warning('Erro na limpeza de comprovantes: %s', e)


def contar_registros():
    try:
        conn = get_db()
        count = conn.execute('SELECT COUNT(*) FROM registros').fetchone()[0]
        conn.close()
        return count
    except Exception as e:
        log.warning('Erro ao contar registros: %s', e)
        return 0


def _render_template(template_name, is_admin=False, page_token=''):
    public_url = os.environ.get('PUBLIC_URL', DOMAIN_PUBLICO).rstrip('/')
    html = open(os.path.join(TEMPLATES_DIR, template_name), 'r', encoding='utf-8').read()
    csrf_token = session.get('csrf_token', '')

    qtd_atingida = contar_registros()

    import urllib.parse
    auto_url = public_url + '/auto-cadastro'

    script = (
        f'<script>'
        f'window.PUBLIC_URL="{public_url}";'
        f'window.DOMAIN="{DOMAIN_PUBLICO}";'
        f'window.PAGE_TOKEN="{page_token}";'
        f'window.CSRF_TOKEN="{csrf_token}";'
        f'window.PIX_KEY="{PIX_CORRETO}";'
        f'window.PRECO_ATE_50={PRECO_ATE_50};'
        f'window.PRECO_ACIMA_50={PRECO_ACIMA_50};'
        f'window.QTD_ATINGIDA={qtd_atingida};'
        f'window.IS_ADMIN={"true" if is_admin else "false"};'
        f'window.AUTO_URL="{auto_url}";'
        f'</script>'
    )
    html = html.replace('</head>', script + '</head>')
    return html, 200, {
        'Content-Type': 'text/html; charset=utf-8',
        'Cache-Control': 'no-cache, no-store, must-revalidate',
        'Pragma': 'no-cache',
        'Expires': '0',
    }


@app.route('/')
def index():
    token = secrets.token_urlsafe(32)
    page_tokens = session.get('page_tokens', {})
    page_tokens[token] = 'admin'
    session['page_tokens'] = page_tokens
    session['role'] = 'admin'
    if 'csrf_token' not in session:
        session['csrf_token'] = secrets.token_urlsafe(32)
    session.permanent = True
    return _render_template('index.html', is_admin=True, page_token=token)


@app.route('/auto-cadastro')
def auto_cadastro():
    token = secrets.token_urlsafe(32)
    page_tokens = session.get('page_tokens', {})
    page_tokens[token] = 'auto_cadastro'
    session['page_tokens'] = page_tokens
    session['role'] = 'auto_cadastro'
    if 'csrf_token' not in session:
        session['csrf_token'] = secrets.token_urlsafe(32)
    session.permanent = True
    return _render_template('auto-cadastro.html', page_token=token)


@app.route('/dados', methods=['GET'])
def api_get_registros():
    if not require_role(admin_only=True):
        return jsonify({'success': False, 'error': 'Não autorizado'}), 401
    data = {
        'registros': ler_registros(),
        'excluidos': ler_excluidos()
    }
    resp = jsonify(data)
    resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    resp.headers['Pragma'] = 'no-cache'
    return resp


@app.route('/salvar', methods=['POST', 'OPTIONS'])
def api_sync_registros():
    try:
        ct = request.content_type or ''
        if 'application/json' in ct:
            incoming = request.get_json()
        else:
            raw = request.data
            if raw:
                incoming = json.loads(raw.decode('utf-8'))
            else:
                incoming = None
        if incoming is None:
            return jsonify({'success': False, 'error': 'Dados inválidos'}), 400

        if isinstance(incoming, list):
            regs = incoming
            excs = []
        elif isinstance(incoming, dict):
            regs = incoming.get('registros', [])
            excs = incoming.get('excluidos', [])
        else:
            return jsonify({'success': False, 'error': 'Formato inválido'}), 400

        if not isinstance(regs, list) or not isinstance(excs, list):
            return jsonify({'success': False, 'error': 'Formato inválido'}), 400

        origem_role = get_current_role(default_to='auto_cadastro')
        is_auto = origem_role == 'auto_cadastro'
        if is_auto:
            for item in regs:
                item['pagamento'] = 'Pendente'

            existing = ler_registros()
            existing_uuids = set(r.get('_uuid', '') for r in existing if r.get('_uuid'))
            excluidos_set = set(ler_excluidos())

            max_num = 0
            for r in existing:
                if r.get('numeros_sorte'):
                    for n in r['numeros_sorte'].split(','):
                        try:
                            max_num = max(max_num, int(n.strip()))
                        except:
                            pass

            max_id = 0
            for r in existing:
                try:
                    max_id = max(max_id, int(r.get('id', '0') or '0'))
                except:
                    pass

            for item in regs:
                item_uuid = item.get('_uuid', '')
                if item_uuid and item_uuid not in existing_uuids and item_uuid not in excluidos_set:
                    qtd = int(item.get('quantidade_pulseiras', '0') or 0)
                    nums = list(range(max_num + 1, max_num + 1 + qtd))
                    item['numeros_sorte'] = ','.join(str(n) for n in nums)
                    item['id'] = str(max_id + 1)
                    max_id += 1
                    max_num += qtd
                    existing.append(item)

            regs = existing
            excs = list(excluidos_set)

        salvar_tudo(regs, excs)

        audit_log('sync', f'{len(regs)} registros, {len(excs)} excluidos, auto={is_auto}')
        backup_db()

        resp = {'success': True}
        if is_auto:
            resp['registros'] = regs
        return jsonify(resp)
    except Exception as e:
        log.error('Erro sync: %s', e)
        return jsonify({'success': False, 'error': str(e)}), 500


def _on_ocr_complete(future, filename):
    try:
        analise, _ = future.result()
        pix_conferido = bool(analise.get('chave_pix_encontrada') and analise.get('chave_pix_encontrada', '').lower() == PIX_CORRETO.lower())
        with ocr_results_lock:
            ocr_results[filename] = {'analise': analise, 'pix_conferido': pix_conferido, 'done': True}
    except Exception as e:
        print('Erro OCR async:', e)
        with ocr_results_lock:
            ocr_results[filename] = {'done': True, 'error': str(e)}


@app.route('/analise/<filename>')
def get_ocr_result(filename):
    if not require_role():
        return jsonify({'status': 'error', 'error': 'Não autorizado'}), 401
    with ocr_results_lock:
        result = ocr_results.get(filename)
    if result is None:
        return jsonify({'status': 'pending', 'done': False})
    return jsonify({'status': 'done' if result.get('done') else 'error', 'analise': result.get('analise'), 'pix_conferido': result.get('pix_conferido', False)})


@app.route('/enviar', methods=['POST'])
def upload_comprovante():
    if not require_role(admin_only=True):
        return jsonify({'success': False, 'error': 'Não autorizado'}), 401
    try:
        if 'file' not in request.files:
            return jsonify({'success': False, 'error': 'Nenhum arquivo enviado'}), 400
        file = request.files['file']
        if file.filename == '':
            return jsonify({'success': False, 'error': 'Nome de arquivo vazio'}), 400
        if not allowed_file(file.filename):
            return jsonify({'success': False, 'error': 'Tipo de arquivo não permitido'}), 400
        if '.' not in file.filename:
            return jsonify({'success': False, 'error': 'Arquivo sem extensão'}), 400

        ext = file.filename.rsplit('.', 1)[1].lower()
        unique_name = str(uuid.uuid4()) + '.' + ext
        filepath = os.path.join(COMPROVANTES_DIR, unique_name)
        file.save(filepath)

        if ext in ('pdf', 'png', 'jpg', 'jpeg', 'gif', 'webp'):
            fn = analisar_pdf if ext == 'pdf' else analisar_imagem
            future = ocr_executor.submit(fn, filepath)
            future.add_done_callback(lambda f, fn=unique_name: _on_ocr_complete(f, fn))

        audit_log('upload', f'{file.filename} -> {unique_name}')

        return jsonify({
            'success': True,
            'filename': unique_name,
            'original_name': file.filename,
            'analise': None,
            'pix_conferido': False
        })
    except Exception as e:
        log.error('Erro no upload: %s', e)
        return jsonify({'success': False, 'error': 'Erro interno no servidor'}), 500


@app.errorhandler(413)
def too_large(error):
    return jsonify({'success': False, 'error': 'Arquivo muito grande. Máximo 10MB.'}), 413


@app.errorhandler(403)
def handle_forbidden(error):
    log.warning('403 disparado: path=%s, method=%s, auth=%s..., ip=%s, session_keys=%s, page_tokens=%s',
                request.path, request.method,
                (request.headers.get('Authorization', '')[:30] + '...') if request.headers.get('Authorization') else 'none',
                request.remote_addr,
                list(session.keys()) if session else 'vazio',
                {k[:15]+'...':v for k,v in session.get('page_tokens', {}).items()})
    if request.path in ('/salvar',):
        return jsonify({'success': True, '_warning': '403 suprimido'}), 200
    return jsonify({'success': False, 'error': 'Acesso negado'}), 403


@app.route('/comprovantes/<filename>')
def servir_comprovante(filename):
    if not require_role():
        return jsonify({'success': False, 'error': 'Não autorizado'}), 401
    safe_path = os.path.realpath(os.path.join(COMPROVANTES_DIR, filename))
    if not safe_path.startswith(os.path.realpath(COMPROVANTES_DIR) + os.sep):
        return jsonify({'success': False, 'error': 'Acesso negado'}), 403
    return send_file(safe_path)


@app.route('/imagem_evento.jpeg')
def servir_imagem():
    return send_file(os.path.join(BASE_DIR, 'imagem_evento.jpeg'))


init_db()
backup_db()

@app.route('/verificar', methods=['GET', 'POST'])
def api_diagnostico():
    import json as _json
    info = {
        'method': request.method,
        'path': request.path,
        'content_type': request.content_type,
        'auth_header': (request.headers.get('Authorization', '')[:40] + '...') if request.headers.get('Authorization') else 'nenhum',
        'origin': request.headers.get('Origin', 'nenhum'),
        'referer': request.headers.get('Referer', 'nenhum'),
        'cookies': dict(request.cookies),
        'session_keys': list(session.keys()) if session else [],
        'page_tokens': {k[:15]+'...':v for k,v in session.get('page_tokens', {}).items()},
        'session_role': session.get('role'),
        'remote_addr': request.remote_addr,
    }
    try:
        raw = request.get_data(as_text=True)
        info['body_preview'] = raw[:200] if raw else '(vazio)'
    except:
        info['body_preview'] = '(erro ao ler)'
    return jsonify({'success': True, 'info': info}), 200, {'Access-Control-Allow-Origin': '*'}


if __name__ == '__main__':
    use_prod = '--prod' in sys.argv

    hostname = socket.gethostname()
    local_ip = socket.gethostbyname(hostname)
    print('=' * 50)
    print('  FESTIVAL MEU ISAQUE E MINHA REBECA')
    print(f'  Token:     {API_TOKEN[:8]}...{API_TOKEN[-4:]}')
    print(f'  Admin:     http://localhost:5000')
    print(f'  Auto-Cad.: http://localhost:5000/auto-cadastro')
    print(f'  Rede:      http://{local_ip}:5000')
    print(f'  Auto-Cad. tambem: http://{local_ip}:5000/auto-cadastro')
    print('=' * 50)

    if use_prod:
        from waitress import serve
        print('  [servidor] Waitress (producao) em http://0.0.0.0:5000')
        print('  [servidor] Para auto-reload, rode sem --prod')
        print('=' * 50)
        serve(app, host='0.0.0.0', port=5000, threads=16)
    else:
        print('  [servidor] Dev mode com auto-reload em http://0.0.0.0:5000')
        print('=' * 50)
        app.run(debug=True, host='0.0.0.0', port=5000)
