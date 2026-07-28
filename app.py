from flask import Flask, render_template, request, jsonify, send_file
import pandas as pd
import tempfile
import os
import uuid
import json
import socket
import random
from ortools.sat.python import cp_model

# === 修复 Windows 中文主机名导致 socket.getfqdn 编码崩溃 ===
_orig_getfqdn = socket.getfqdn
def _patched_getfqdn(name=''):
    try:
        return _orig_getfqdn(name)
    except UnicodeDecodeError:
        return name or 'localhost'
socket.getfqdn = _patched_getfqdn

app = Flask(__name__, template_folder=os.path.join(os.path.dirname(os.path.abspath(__file__)), 'templates'))
app.jinja_env.bytecode_cache = None  # 禁用模板缓存，确保每次读取最新文件

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

# 临时存储下载文件
downloads = {}

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/solve', methods=['POST'])
def solve():
    if 'file' not in request.files:
        return jsonify({'error': '未上传文件'}), 400
    file = request.files['file']
    if file.filename == '':
        return jsonify({'error': '文件名为空'}), 400

    try:
        total_weight_min = float(request.form.get('total_weight_min', 0))
        total_weight_max = float(request.form.get('total_weight_max', 0) or 0)
        target_jelly = float(request.form.get('target_jelly', 0))
        jelly_filter_str = request.form.get('jelly_filter', '')
        num_solutions = int(request.form.get('num_solutions', 1))
        num_solutions = max(1, min(num_solutions, 10))  # 限制 1~10
        no_batch_overlap = request.form.get('no_batch_overlap', '0') == '1'
        limits = json.loads(request.form.get('limits', '{}'))
        # 解析单行冻力筛选范围，格式: "100-200, 300-400"
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
        df = pd.read_excel(file, engine='openpyxl')
    except Exception as e:
        return jsonify({'error': f'Excel读取失败: {str(e)}'}), 400

    # === 清洗列名：去首尾空格，去除换行符 ===
    df.columns = [str(col).strip().replace('\n', '').replace('\r', '') for col in df.columns]

    # 仅保留你需要的7列
    required_cols = [COL_BATCH, COL_WEIGHT, COL_JELLY, COL_WATER, COL_ASH, COL_VISCOSITY, COL_TRANS450, COL_TRANS620]

    # 检查这些列是否都存在
    missing_cols = [col for col in required_cols if col not in df.columns]
    if missing_cols:
        return jsonify({
            'error': f'缺少必需列: {", ".join(missing_cols)}。\n'
                     f'清洗后Excel列名为: {", ".join(df.columns.tolist())}'
        }), 400

    # 只保留需要的列，忽略其他所有列
    df = df[required_cols].copy()

    # 单行冻力多段筛选：只保留冻力落在任一范围内的行
    if jelly_filter_ranges:
        mask = pd.Series(False, index=df.index)
        for lo, hi in jelly_filter_ranges:
            mask = mask | ((df[COL_JELLY] >= lo) & (df[COL_JELLY] <= hi))
        df = df[mask].copy()
    else:
        # 默认：单值视为上限
        df = df[df[COL_JELLY] <= float(request.form.get('jelly_filter', 190))].copy()

    if len(df) == 0:
        return jsonify({'error': '经过冻力筛选后无数据，请放宽单行冻力上限'}), 400

    for col in [COL_WEIGHT] + INDICATORS:
        df[col] = pd.to_numeric(df[col], errors='coerce')
    df.dropna(subset=[COL_WEIGHT] + INDICATORS, inplace=True)
    if len(df) == 0:
        return jsonify({'error': '有效数值行为0'}), 400

    # 筛选后索引可能不连续，重置为 0,1,2...
    df.reset_index(drop=True, inplace=True)

    SCALE = 100
    weights = (df[COL_WEIGHT] * SCALE).astype(int)
    jelly = (df[COL_JELLY] * SCALE).astype(int)
    water = (df[COL_WATER] * SCALE).astype(int)
    ash = (df[COL_ASH] * SCALE).astype(int)
    viscosity = (df[COL_VISCOSITY] * SCALE).astype(int)
    trans450 = (df[COL_TRANS450] * SCALE).astype(int)
    trans620 = (df[COL_TRANS620] * SCALE).astype(int)

    # ==================== 多方案求解 ====================
    n = len(df)
    target_low = int(target_jelly * SCALE)
    target_high = int(target_jelly * 1.05 * SCALE)
    has_upper = total_weight_max > 0

    all_solutions = []
    prev_selections = []
    all_used_indices = set()  # 批道不重复模式：累计所有已用行

    for sol_idx in range(num_solutions):
        model = cp_model.CpModel()
        x = [model.NewBoolVar(f'x{i}') for i in range(n)]
        sum_weight = sum(weights[i] * x[i] for i in range(n))

        # 总重范围
        model.Add(sum_weight >= int(total_weight_min * SCALE))
        if has_upper:
            model.Add(sum_weight <= int(total_weight_max * SCALE))
            # 有上限：随机目标重量分散在 [min, max] 区间
            random_target = random.uniform(total_weight_min, total_weight_max)
            slack = max(50, (total_weight_max - total_weight_min) * 0.1)
            model.Add(sum_weight >= int((random_target - slack) * SCALE))
            model.Add(sum_weight <= int((random_target + slack) * SCALE))
        else:
            # 无上限：最小化总重，方案自动贴近下限
            model.Minimize(sum_weight)

        # 冻力约束
        sum_jelly = sum(jelly[i] * weights[i] * x[i] for i in range(n))
        model.Add(sum_jelly - target_low * sum_weight >= 0)
        model.Add(target_high * sum_weight - sum_jelly >= 0)

        # 其他指标约束
        for ind_name, ind_vals in [(COL_WATER, water), (COL_ASH, ash), (COL_VISCOSITY, viscosity), (COL_TRANS450, trans450), (COL_TRANS620, trans620)]:
            low, high = limits.get(ind_name, (None, None))
            if low is not None or high is not None:
                sum_ind = sum(ind_vals[i] * weights[i] * x[i] for i in range(n))
                if low is not None:
                    model.Add(sum_ind - int(low * SCALE) * sum_weight >= 0)
                if high is not None:
                    model.Add(int(high * SCALE) * sum_weight - sum_ind >= 0)

        # 排除之前找到的方案
        if no_batch_overlap:
            # 批道不重复：禁止选用之前方案用过的任何行
            model.Add(sum(x[i] for i in all_used_indices) == 0)
        else:
            # 普通模式：只需至少一行不同
            for prev in prev_selections:
                model.Add(sum(x[i] for i in prev) <= len(prev) - 1)

        solver = cp_model.CpSolver()
        solver.parameters.max_time_in_seconds = 10
        status = solver.Solve(model)

        if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            continue  # 这个随机目标无解，跳过试下一个

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
        cols_order = [COL_BATCH, COL_WEIGHT, COL_JELLY, COL_WATER, COL_ASH, COL_VISCOSITY, COL_TRANS450, COL_TRANS620, '选用重量kg']
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
    # ==================== 多方案求解结束 ====================

    if not all_solutions:
        return jsonify({'error': '无可行方案，请放宽约束条件'}), 400

    return jsonify({
        'success': True,
        'count': len(all_solutions),
        'solutions': all_solutions
    })

@app.route('/download/<file_id>')
def download_file(file_id):
    path = downloads.get(file_id)
    if not path or not os.path.exists(path):
        return '文件不存在', 404
    return send_file(path, as_attachment=True, download_name='物料组合方案.xlsx')

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False)
