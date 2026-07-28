from flask import Flask, render_template, request, jsonify, send_file, Response
import pandas as pd
import tempfile
import os
import uuid
import json
import socket
import sys
import webbrowser
import threading
import random
import time
from ortools.sat.python import cp_model

# === 修复 Windows 中文主机名导致 socket.getfqdn 编码崩溃 ===
_orig_getfqdn = socket.getfqdn
def _patched_getfqdn(name=''):
    try:
        return _orig_getfqdn(name)
    except UnicodeDecodeError:
        return name or 'localhost'
socket.getfqdn = _patched_getfqdn

def resource_path(relative_path):
    if getattr(sys, 'frozen', False):
        return os.path.join(sys._MEIPASS, relative_path)
    return os.path.join(os.path.abspath('.'), relative_path)

app = Flask(__name__, template_folder=resource_path('templates'))

# 固定列名
COL_BATCH = "批道"
COL_WEIGHT = "总数kg"
COL_JELLY = "冻力"
COL_WATER = "水分"
COL_ASH = "灰分"
COL_VISCOSITY = "勃氏粘度"
COL_TRANS450 = "透光率450"
COL_TRANS620 = "透光率620"

INDICATORS = [COL_JELLY, COL_WATER, COL_ASH, COL_VISCOSITY, COL_TRANS450, COL_TRANS620]

# 临时存储
downloads = {}
tasks = {}
tasks_lock = threading.Lock()

@app.route('/')
def index():
    return render_template('index.html')

def _solve_task(task_id, df, total_weight_min, total_weight_max, jelly_low, jelly_high,
                num_solutions, no_batch_overlap, limits):
    """后台线程：执行 OR-Tools 求解，结果写入 tasks[task_id]"""
    SCALE = 100
    weights = (df[COL_WEIGHT] * SCALE).astype(int)
    jelly = (df[COL_JELLY] * SCALE).astype(int)
    water = (df[COL_WATER] * SCALE).astype(int)
    ash = (df[COL_ASH] * SCALE).astype(int)
    viscosity = (df[COL_VISCOSITY] * SCALE).astype(int)
    trans450 = (df[COL_TRANS450] * SCALE).astype(int)
    trans620 = (df[COL_TRANS620] * SCALE).astype(int)

    n = len(df)
    target_low = int(jelly_low * SCALE)
    target_high = int(jelly_high * SCALE)
    has_upper = total_weight_max > 0

    all_solutions = []
    prev_selections = []
    all_used_indices = set()

    for sol_idx in range(num_solutions):
        model = cp_model.CpModel()
        x = [model.NewBoolVar(f'x{i}') for i in range(n)]
        sum_weight = sum(weights[i] * x[i] for i in range(n))

        model.Add(sum_weight >= int(total_weight_min * SCALE))
        if has_upper:
            model.Add(sum_weight <= int(total_weight_max * SCALE))
            random_target = random.uniform(total_weight_min, total_weight_max)
            slack = max(50, (total_weight_max - total_weight_min) * 0.1)
            model.Add(sum_weight >= int((random_target - slack) * SCALE))
            model.Add(sum_weight <= int((random_target + slack) * SCALE))
        else:
            model.Minimize(sum_weight)

        sum_jelly = sum(jelly[i] * weights[i] * x[i] for i in range(n))
        model.Add(sum_jelly - target_low * sum_weight >= 0)
        model.Add(target_high * sum_weight - sum_jelly >= 0)

        for ind_name, ind_vals in [(COL_WATER, water), (COL_ASH, ash), (COL_VISCOSITY, viscosity), (COL_TRANS450, trans450), (COL_TRANS620, trans620)]:
            low, high = limits.get(ind_name, (None, None))
            if low is not None or high is not None:
                sum_ind = sum(ind_vals[i] * weights[i] * x[i] for i in range(n))
                if low is not None:
                    model.Add(sum_ind - int(low * SCALE) * sum_weight >= 0)
                if high is not None:
                    model.Add(int(high * SCALE) * sum_weight - sum_ind >= 0)

        if no_batch_overlap:
            model.Add(sum(x[i] for i in all_used_indices) == 0)
        else:
            for prev in prev_selections:
                model.Add(sum(x[i] for i in prev) <= len(prev) - 1)

        solver = cp_model.CpSolver()
        solver.parameters.max_time_in_seconds = 10
        status = solver.Solve(model)

        if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            # 更新进度
            with tasks_lock:
                if task_id in tasks:
                    tasks[task_id]['progress'] = f'{sol_idx}/{num_solutions}'
                    tasks[task_id]['found'] = len(all_solutions)
            continue

        selected = []
        total_weight_val = 0.0
        used = []
        for i in range(n):
            if solver.Value(x[i]) == 1:
                w = weights[i] / SCALE
                selected.append(i)
                used.append(w)
                total_weight_val += w

        prev_selections.append(selected)
        if no_batch_overlap:
            all_used_indices.update(selected)

        def weighted_avg(col_name):
            s = sum(df.iloc[i][col_name] * used[idx] for idx, i in enumerate(selected))
            return round(s / total_weight_val, 2)

        final_indicators = {ind: weighted_avg(ind) for ind in INDICATORS}

        result_rows = []
        for idx, i in enumerate(selected):
            row_data = df.iloc[i].to_dict()
            row_data['选用重量kg'] = used[idx]
            result_rows.append(row_data)

        result_df = pd.DataFrame(result_rows)
        cols_order = [COL_BATCH, COL_WEIGHT, COL_JELLY, COL_WATER, COL_ASH, COL_VISCOSITY, COL_TRANS450, COL_TRANS620]
        if COL_PROD_DATE_G:
            cols_order.append(COL_PROD_DATE_G)
        cols_order.append('选用重量kg')
        result_df = result_df[[c for c in cols_order if c in result_df.columns]]
        file_id = str(uuid.uuid4())
        path = os.path.join(tempfile.gettempdir(), f'{file_id}.xlsx')
        result_df.to_excel(path, index=False, engine='openpyxl')
        downloads[file_id] = path

        all_solutions.append({
            'total_weight': round(total_weight_val, 2),
            'final_indicators': final_indicators,
            'result_rows': result_rows,
            'download_id': file_id
        })

        # 更新进度
        with tasks_lock:
            if task_id in tasks:
                tasks[task_id]['progress'] = f'{sol_idx + 1}/{num_solutions}'
                tasks[task_id]['found'] = len(all_solutions)
                tasks[task_id]['partial'] = list(all_solutions)

    # 完成
    with tasks_lock:
        if task_id in tasks:
            tasks[task_id]['done'] = True
            tasks[task_id]['solutions'] = all_solutions
            tasks[task_id]['found'] = len(all_solutions)

# 全局变量存储生产日期列名（在 /solve 中设置，在 _solve_task 中使用）
COL_PROD_DATE_G = None

@app.route('/solve', methods=['POST'])
def solve():
    global COL_PROD_DATE_G
    if 'file' not in request.files:
        return jsonify({'error': '未上传文件'}), 400
    file = request.files['file']
    if file.filename == '':
        return jsonify({'error': '文件名为空'}), 400

    try:
        header_row = int(request.form.get('header_row', 1))
        header_row = max(1, min(header_row, 10))
        total_weight_min = float(request.form.get('total_weight_min', 0))
        total_weight_max = float(request.form.get('total_weight_max', 0) or 0)
        jelly_filter_str = request.form.get('jelly_filter', '')
        num_solutions = int(request.form.get('num_solutions', 1))
        num_solutions = max(1, min(num_solutions, 10))
        no_batch_overlap = request.form.get('no_batch_overlap', '0') == '1'
        limits = json.loads(request.form.get('limits', '{}'))

        jelly_low_str = request.form.get('jelly_low', '')
        jelly_high_str = request.form.get('jelly_high', '')
        if jelly_low_str and jelly_high_str:
            jelly_low = float(jelly_low_str)
            jelly_high = float(jelly_high_str)
        else:
            target_jelly = float(request.form.get('target_jelly', 0))
            jelly_low = target_jelly
            jelly_high = target_jelly * 1.05

        salmonella_filter = request.form.get('salmonella_filter', '').strip()
        coli_filter = request.form.get('coli_filter', '').strip()
        halal_filter = request.form.get('halal_filter', '').strip()

        jelly_filter_ranges = []
        for part in jelly_filter_str.replace('，', ',').replace('到', '-').replace('~', '-').split(','):
            part = part.strip()
            if not part:
                continue
            if '-' in part:
                a, b = part.split('-', 1)
                jelly_filter_ranges.append((float(a.strip()), float(b.strip())))
            else:
                v = float(part)
                jelly_filter_ranges.append((0, v))
    except Exception as e:
        return jsonify({'error': f'参数解析错误: {str(e)}'}), 400

    try:
        df = pd.read_excel(file, engine='openpyxl', header=header_row - 1)
    except Exception as e:
        return jsonify({'error': f'Excel读取失败: {str(e)}'}), 400

    df.columns = [str(col).strip().replace('\n', '').replace('\r', '') for col in df.columns]

    required_cols = [COL_BATCH, COL_WEIGHT, COL_JELLY, COL_WATER, COL_ASH, COL_VISCOSITY, COL_TRANS450, COL_TRANS620]

    # 自动识别生产日期列
    COL_PROD_DATE_G = None
    for col in df.columns:
        if '生产日期' in str(col) or '日期' in str(col) or 'date' in str(col).lower():
            COL_PROD_DATE_G = col
            break

    missing_cols = [col for col in required_cols if col not in df.columns]
    if missing_cols:
        return jsonify({
            'error': f'缺少必需列: {", ".join(missing_cols)}。\n清洗后Excel列名为: {", ".join(df.columns.tolist())}'
        }), 400

    keep_cols = required_cols.copy()
    if COL_PROD_DATE_G:
        keep_cols.append(COL_PROD_DATE_G)
    df = df[[c for c in keep_cols if c in df.columns]].copy()

    # 生产日期转为纯数字 YYYYMMDD
    if COL_PROD_DATE_G and COL_PROD_DATE_G in df.columns:
        df[COL_PROD_DATE_G] = pd.to_datetime(df[COL_PROD_DATE_G], errors='coerce')
        df[COL_PROD_DATE_G] = df[COL_PROD_DATE_G].apply(
            lambda x: str(int(x.strftime('%Y%m%d'))) if pd.notna(x) else ''
        )

    # 单行冻力多段筛选
    if jelly_filter_ranges:
        mask = pd.Series(False, index=df.index)
        for lo, hi in jelly_filter_ranges:
            mask = mask | ((df[COL_JELLY] >= lo) & (df[COL_JELLY] <= hi))
        df = df[mask].copy()
    else:
        df = df[df[COL_JELLY] <= jelly_high].copy()

    if len(df) == 0:
        return jsonify({'error': '经过冻力筛选后无数据，请放宽单行冻力上限'}), 400

    # 沙门发酵筛选
    salmonella_col = None
    for col in df.columns:
        if '沙门' in str(col) or 'salmonella' in str(col).lower():
            salmonella_col = col
            break
    if salmonella_filter and salmonella_col:
        filter_vals = [x.strip() for x in salmonella_filter.replace('，', ',').split(',') if x.strip()]
        if filter_vals:
            df = df[df[salmonella_col].astype(str).str.contains('|'.join(filter_vals), na=False)].copy()

    # 大脑发酵筛选
    coli_col = None
    for col in df.columns:
        if '大脑' in str(col) or 'coli' in str(col).lower() or '大肠' in str(col):
            coli_col = col
            break
    if coli_filter and coli_col:
        filter_vals = [x.strip() for x in coli_filter.replace('，', ',').split(',') if x.strip()]
        if filter_vals:
            df = df[df[coli_col].astype(str).str.contains('|'.join(filter_vals), na=False)].copy()

    # 清真筛选
    if halal_filter == 'halal':
        df = df[df[COL_BATCH].astype(str).str.lower().str.startswith('c02b')].copy()
    elif halal_filter == 'non_halal':
        df = df[~df[COL_BATCH].astype(str).str.lower().str.startswith('c02b')].copy()

    if len(df) == 0:
        return jsonify({'error': '经过筛选后无数据，请放宽筛选条件'}), 400

    for col in [COL_WEIGHT] + INDICATORS:
        df[col] = pd.to_numeric(df[col], errors='coerce')
    df.dropna(subset=[COL_WEIGHT] + INDICATORS, inplace=True)
    if len(df) == 0:
        return jsonify({'error': '有效数值行为0'}), 400

    df.reset_index(drop=True, inplace=True)

    # 创建后台任务
    task_id = str(uuid.uuid4())
    with tasks_lock:
        tasks[task_id] = {
            'done': False,
            'found': 0,
            'progress': f'0/{num_solutions}',
            'solutions': [],
            'partial': [],
            'total': num_solutions,
            'row_count': len(df)
        }

    thread = threading.Thread(
        target=_solve_task,
        args=(task_id, df.copy(), total_weight_min, total_weight_max, jelly_low, jelly_high,
              num_solutions, no_batch_overlap, limits),
        daemon=True
    )
    thread.start()

    return jsonify({'task_id': task_id, 'row_count': len(df), 'total_solutions': num_solutions})

@app.route('/progress/<task_id>')
def get_progress(task_id):
    with tasks_lock:
        t = tasks.get(task_id)
    if not t:
        return jsonify({'error': '任务不存在'}), 404
    return jsonify({
        'done': t['done'],
        'found': t['found'],
        'progress': t['progress'],
        'total': t['total'],
        'row_count': t.get('row_count', 0)
    })

@app.route('/result/<task_id>')
def get_result(task_id):
    with tasks_lock:
        t = tasks.get(task_id)
    if not t:
        return jsonify({'error': '任务不存在'}), 404
    if not t['done']:
        return jsonify({'done': False, 'found': t['found'], 'progress': t['progress']})
    if not t['solutions']:
        return jsonify({'error': '无可行方案，请放宽约束条件'})
    return jsonify({
        'done': True,
        'success': True,
        'count': len(t['solutions']),
        'solutions': t['solutions']
    })

@app.route('/download/<file_id>')
def download_file(file_id):
    path = downloads.get(file_id)
    if not path or not os.path.exists(path):
        return '文件不存在', 404
    return send_file(path, as_attachment=True, download_name='物料组合方案.xlsx')

@app.route('/save-excel', methods=['POST'])
def save_excel():
    try:
        data = request.get_json()
        rows = data.get('rows', [])
        filename = data.get('filename', '处理后总表')
        if not rows:
            return jsonify({'error': '数据为空'}), 400

        df = pd.DataFrame(rows)
        desktop = os.path.join(os.path.expanduser('~'), 'Desktop')
        if not os.path.exists(desktop):
            desktop = os.path.join(os.path.expanduser('~'), '桌面')
        os.makedirs(desktop, exist_ok=True)

        base = filename
        counter = 1
        save_path = os.path.join(desktop, f'{base}.xlsx')
        while os.path.exists(save_path):
            save_path = os.path.join(desktop, f'{base}_{counter}.xlsx')
            counter += 1

        df.to_excel(save_path, index=False, engine='openpyxl')
        return jsonify({'success': True, 'path': save_path, 'filename': os.path.basename(save_path)})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/preview-rows', methods=['POST'])
def preview_rows():
    """预览筛选后行数（不解）"""
    try:
        data = request.get_json()
        row_count = int(data.get('row_count', 0))
        preview_count = min(data.get('preview_count', 10), 50)
        return jsonify({
            'row_count': row_count,
            'message': f'共 {row_count} 行可用于求解'
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    def open_browser():
        webbrowser.open('http://127.0.0.1:5000')
    threading.Timer(1.5, open_browser).start()
    app.run(host='0.0.0.0', port=5000, debug=False)
