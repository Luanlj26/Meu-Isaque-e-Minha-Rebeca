import os
import sys
import socket
import uuid
import re
import json
import sqlite3
import secrets
import threading
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor
from flask import Flask, jsonify, request, send_file
from PIL import Image
import fitz

try:
    import pytesseract
    pytesseract.pytesseract.tesseract_cmd = r'C:\Program Files\Tesseract-OCR\tesseract.exe'
    os.environ['TESSDATA_PREFIX'] = os.path.join(os.environ['LOCALAPPDATA'], 'Tesseract-OCR', 'tessdata')
    TESSERACT_DISPONIVEL = True
except Exception:
    TESSERACT_DISPONIVEL = False

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TEMPLATES_DIR = os.path.join(BASE_DIR, 'templates')
COMPROVANTES_DIR = os.path.join(BASE_DIR, 'comprovantes')
DATABASE = os.path.join(BASE_DIR, 'database.db')

app = Flask(__name__)
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
    response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type, Authorization'
    response.headers['Access-Control-Allow-Methods'] = 'GET, POST, PUT, DELETE, OPTIONS'
    response.headers['Access-Control-Max-Age'] = '86400'
    return response


@app.before_request
def handle_options():
    if request.method == 'OPTIONS':
        return jsonify({}), 200


app.config['MAX_CONTENT_LENGTH'] = 10 * 1024 * 1024

ALLOWED_EXTENSIONS = {'pdf'}

PIX_CORRETO = 'luanborges26@outlook.com'


def require_token():
    auth = request.headers.get('Authorization', '')
    if auth.startswith('Bearer ') and auth[7:] == API_TOKEN:
        return True
    if request.form.get('_token') == API_TOKEN:
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
                except:
                    pass
    except:
        pass


def _render_template(template_name):
    public_url = os.environ.get('PUBLIC_URL', '')
    html = open(os.path.join(TEMPLATES_DIR, template_name), 'r', encoding='utf-8').read()
    script = f'<script>window.PUBLIC_URL="{public_url}";window.API_TOKEN="{API_TOKEN}";</script>'
    html = html.replace('</head>', script + '</head>')
    return html, 200, {'Content-Type': 'text/html; charset=utf-8'}


@app.route('/')
def index():
    return _render_template('index.html')


@app.route('/auto-cadastro')
def auto_cadastro():
    return _render_template('auto-cadastro.html')


@app.route('/api/registros', methods=['GET'])
def api_get_registros():
    if not require_token():
        return jsonify({'success': False, 'error': 'Não autorizado'}), 401
    data = {
        'registros': ler_registros(),
        'excluidos': ler_excluidos()
    }
    resp = jsonify(data)
    resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    resp.headers['Pragma'] = 'no-cache'
    return resp


@app.route('/api/sync-registros', methods=['POST', 'OPTIONS'])
def api_sync_registros():
    if not require_token():
        return jsonify({'success': False, 'error': 'Não autorizado'}), 401
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

        salvar_tudo(regs, excs)

        return jsonify({'success': True})
    except Exception as e:
        print('Erro sync:', e)
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


@app.route('/api/ocr/<filename>')
def get_ocr_result(filename):
    with ocr_results_lock:
        result = ocr_results.get(filename)
    if result is None:
        return jsonify({'status': 'pending', 'done': False})
    return jsonify({'status': 'done' if result.get('done') else 'error', 'analise': result.get('analise'), 'pix_conferido': result.get('pix_conferido', False)})


@app.route('/api/upload-form', methods=['POST'])
def upload_comprovante_form():
    if not require_token():
        return _redirect_result({'success': False, 'error': 'Não autorizado', 'id': request.form.get('id', '0')})
    registro_id = request.form.get('id', '0')
    try:
        if 'file' not in request.files:
            return _redirect_result({'success': False, 'error': 'Nenhum arquivo', 'id': registro_id})
        file = request.files['file']
        if file.filename == '':
            return _redirect_result({'success': False, 'error': 'Arquivo vazio', 'id': registro_id})
        if not allowed_file(file.filename):
            return _redirect_result({'success': False, 'error': 'Tipo não permitido', 'id': registro_id})
        if '.' not in file.filename:
            return _redirect_result({'success': False, 'error': 'Sem extensão', 'id': registro_id})

        ext = file.filename.rsplit('.', 1)[1].lower()
        unique_name = str(uuid.uuid4()) + '.' + ext
        filepath = os.path.join(COMPROVANTES_DIR, unique_name)
        file.save(filepath)

        if ext in ('pdf', 'png', 'jpg', 'jpeg', 'gif', 'webp'):
            fn = analisar_pdf if ext == 'pdf' else analisar_imagem
            future = ocr_executor.submit(fn, filepath)
            future.add_done_callback(lambda f, fn=unique_name: _on_ocr_complete(f, fn))

        return _redirect_result({
            'success': True, 'id': registro_id, 'filename': unique_name,
            'original_name': file.filename, 'analise': None, 'pix_conferido': False
        })
    except Exception as e:
        print('Erro upload form:', e)
        return _redirect_result({'success': False, 'error': 'Erro interno', 'id': registro_id})


def _redirect_result(data):
    js = json.dumps(data, default=str)
    html = '<!DOCTYPE html><html><head><script>try{localStorage.setItem("upload_pending",' + json.dumps(js) + ')}catch(e){}window.location.replace("/?upload_done=1");</script></head><body></body></html>'
    return html, 200, {'Content-Type': 'text/html; charset=utf-8'}


@app.route('/api/upload', methods=['POST'])
def upload_comprovante():
    if not require_token():
        return jsonify({'success': False, 'error': 'Não autorizado'}), 401
    try:
        def resposta(data, status=200):
            return jsonify(data), status
        if 'file' not in request.files:
            return resposta({'success': False, 'error': 'Nenhum arquivo enviado'}, 400)
        file = request.files['file']
        if file.filename == '':
            return resposta({'success': False, 'error': 'Nome de arquivo vazio'}, 400)
        if not allowed_file(file.filename):
            return resposta({'success': False, 'error': 'Tipo de arquivo não permitido'}, 400)

        if '.' not in file.filename:
            return resposta({'success': False, 'error': 'Arquivo sem extensão'}, 400)

        ext = file.filename.rsplit('.', 1)[1].lower()
        unique_name = str(uuid.uuid4()) + '.' + ext
        filepath = os.path.join(COMPROVANTES_DIR, unique_name)
        file.save(filepath)

        if ext in ('pdf', 'png', 'jpg', 'jpeg', 'gif', 'webp'):
            fn = analisar_pdf if ext == 'pdf' else analisar_imagem
            future = ocr_executor.submit(fn, filepath)
            future.add_done_callback(lambda f, fn=unique_name: _on_ocr_complete(f, fn))

        return resposta({
            'success': True,
            'filename': unique_name,
            'original_name': file.filename,
            'analise': None,
            'pix_conferido': False
        })
    except Exception as e:
        print('Erro no upload:', e)
        return resposta({'success': False, 'error': 'Erro interno no servidor'}, 500)


@app.errorhandler(413)
def too_large(error):
    return jsonify({'success': False, 'error': 'Arquivo muito grande. Máximo 10MB.'}), 413


@app.route('/comprovantes/<filename>')
def servir_comprovante(filename):
    return send_file(os.path.join(COMPROVANTES_DIR, filename))


@app.route('/imagem_evento.jpeg')
def servir_imagem():
    return send_file(os.path.join(BASE_DIR, 'imagem_evento.jpeg'))


@app.route('/api/qr-code')
def gerar_qrcode():
    import io
    import qrcode
    from PIL import Image as PILImage
    data = request.args.get('data', '')
    if not data:
        return 'Par\u00e2metro "data" \u00e9 obrigat\u00f3rio', 400
    size = request.args.get('size', '10')
    try:
        box_size = max(3, min(50, int(size)))
    except:
        box_size = 10
    qr = qrcode.QRCode(box_size=box_size, border=2)
    qr.add_data(data)
    qr.make(fit=True)
    img = qr.make_image(fill_color='black', back_color='white')
    buf = io.BytesIO()
    img.save(buf, format='PNG')
    buf.seek(0)
    return send_file(buf, mimetype='image/png')


NGROK_TOKEN_FILE = os.path.join(BASE_DIR, 'ngrok_token.txt')

SUBDOMINIO_NGROK = 'festival-isaeque-rebeca'


def iniciar_ngrok():
    try:
        from pyngrok import ngrok, conf
    except ImportError:
        print('  [ngrok] Instalando pyngrok...')
        os.system('pip install pyngrok -q')
        try:
            from pyngrok import ngrok, conf
        except:
            print('  [ngrok] Falha ao instalar pyngrok. Instale manualmente: pip install pyngrok')
            return None

    if os.path.exists(NGROK_TOKEN_FILE):
        with open(NGROK_TOKEN_FILE, 'r') as f:
            token = f.read().strip()
            if token:
                try:
                    conf.get_default().auth_token = token
                except:
                    pass

    def conectar_ngrok():
        try:
            return ngrok.connect(5000, domain=f'{SUBDOMINIO_NGROK}.ngrok-free.app')
        except:
            try:
                return ngrok.connect(5000, subdomain=SUBDOMINIO_NGROK)
            except:
                return ngrok.connect(5000)

    try:
        tunnel = conectar_ngrok()
        return tunnel.public_url
    except Exception as e:
        if 'auth' in str(e).lower():
            print('\n  [ngrok] Token nao configurado.')
            print('  [ngrok] Crie conta: https://dashboard.ngrok.com/signup')
            print('  [ngrok] Pegue o token: https://dashboard.ngrok.com/get-started/your-authtoken\n')
            token = input('  Cole seu token ngrok (apenas 1 vez): ').strip()
            if token:
                try:
                    conf.get_default().auth_token = token
                    with open(NGROK_TOKEN_FILE, 'w') as f:
                        f.write(token)
                    print('  [ngrok] Token salvo em ngrok_token.txt')
                    tunnel = conectar_ngrok()
                    return tunnel.public_url
                except Exception as e2:
                    print('  [ngrok] Erro ao configurar token:', e2)
        else:
            print('  [ngrok] Erro:', e)
        return None


init_db()

if __name__ == '__main__':
    use_online = '--online' in sys.argv
    use_prod = '--prod' in sys.argv

    hostname = socket.gethostname()
    local_ip = socket.gethostbyname(hostname)
    print('=' * 50)
    print('  FESTIVAL MEU ISAQUE E MINHA REBECA')
    print(f'  Token:     {API_TOKEN}')
    print(f'  Admin:     http://localhost:5000')
    print(f'  Auto-Cad.: http://localhost:5000/auto-cadastro')
    print(f'  Rede:      http://{local_ip}:5000')

    public_url = None
    if use_online:
        print('  [ngrok] Ativando túnel público...')
        public_url = iniciar_ngrok()
        if public_url:
            print(f'  PUBLICO:   {public_url}')
            print(f'  PUBLICO:   {public_url}/auto-cadastro')
            os.environ['PUBLIC_URL'] = public_url
        else:
            print('  [ngrok] Falha ao criar túnel. Usando apenas rede local.')
    print('=' * 50)

    if use_prod:
        from waitress import serve
        print('  [servidor] Waitress (produção) em http://0.0.0.0:5000')
        print('=' * 50)
        serve(app, host='0.0.0.0', port=5000, threads=16)
    else:
        app.run(debug=False, host='0.0.0.0', port=5000)
