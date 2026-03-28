import os
import uuid
from datetime import datetime
from flask import Flask, render_template, request, redirect, url_for, send_from_directory, flash, jsonify
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import or_

app = Flask(__name__)
app.secret_key = 'law_firm_secret_key_123'

# --- 配置区 ---
UPLOAD_FOLDER = 'uploads'
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///files.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['MAX_CONTENT_LENGTH'] = 500 * 1024 * 1024 

db = SQLAlchemy(app)

if not os.path.exists(UPLOAD_FOLDER):
    os.makedirs(UPLOAD_FOLDER)

# --- 数据库模型 ---
class Folder(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    create_time = db.Column(db.DateTime, default=datetime.now)
    parent_id = db.Column(db.Integer, db.ForeignKey('folder.id'), nullable=True)
    children = db.relationship('Folder', backref=db.backref('parent', remote_side=[id]), lazy=True)

class FileRecord(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    real_filename = db.Column(db.String(200), nullable=False)
    disk_filename = db.Column(db.String(200), nullable=False)
    filepath = db.Column(db.String(200), nullable=False)
    upload_time = db.Column(db.DateTime, default=datetime.now)
    note = db.Column(db.String(200), nullable=True)
    folder_id = db.Column(db.Integer, db.ForeignKey('folder.id'), nullable=True)

# ==========================================
# 🌟 新增：智能重命名辅助函数 (防止同名冲突)
# ==========================================

def get_unique_foldername(parent_id, folder_name):
    """如果文件夹同名，自动生成 文件夹(1), 文件夹(2)"""
    new_name = folder_name
    counter = 1
    # 只要数据库里还能查到这个名字，数字就一直往上加
    while Folder.query.filter_by(parent_id=parent_id, name=new_name).first():
        new_name = f"{folder_name}({counter})"
        counter += 1
    return new_name

def get_unique_filename(folder_id, filename):
    """如果文件同名，自动生成 文件(1).pdf, 文件(2).pdf"""
    base_name, ext = os.path.splitext(filename)
    new_name = filename
    counter = 1
    # 只要数据库里还能查到这个名字，就在后缀名前面加数字
    while FileRecord.query.filter_by(folder_id=folder_id, real_filename=new_name).first():
        new_name = f"{base_name}({counter}){ext}"
        counter += 1
    return new_name

def get_or_create_subfolder(parent_id, folder_name):
    """用于上传文件夹时：如果有同名文件夹，直接合并进现有文件夹"""
    existing = Folder.query.filter_by(parent_id=parent_id, name=folder_name).first()
    if existing: 
        return existing.id
    new_folder = Folder(name=folder_name, parent_id=parent_id)
    db.session.add(new_folder)
    db.session.commit()
    return new_folder.id

def delete_folder_contents_recursive(folder):
    files = FileRecord.query.filter_by(folder_id=folder.id).all()
    for f in files:
        try: os.remove(f.filepath)
        except: pass
        db.session.delete(f)
    subfolders = Folder.query.filter_by(parent_id=folder.id).all()
    for sub in subfolders:
        delete_folder_contents_recursive(sub)
        db.session.delete(sub)

# --- 路由 ---
@app.route('/', methods=['GET'])
def index():
    folder_id = request.args.get('folder_id', type=int)
    query = request.args.get('q', '')
    sort_by = request.args.get('sort', 'date_desc')
    
    current_folder = None
    breadcrumbs = []
    if folder_id:
        current_folder = Folder.query.get(folder_id)
        if current_folder:
            curr = current_folder
            while curr:
                breadcrumbs.insert(0, curr)
                curr = Folder.query.get(curr.parent_id) if curr.parent_id else None

    files = []
    folders = []
    
    if query:
        files = FileRecord.query.filter(or_(FileRecord.real_filename.contains(query), FileRecord.note.contains(query))).all()
        folders = Folder.query.filter(Folder.name.contains(query)).all()
    else:
        folders_q = Folder.query.filter_by(parent_id=folder_id).order_by(Folder.create_time.desc())
        files_q = FileRecord.query.filter_by(folder_id=folder_id)

        if sort_by == 'name_asc': files_q = files_q.order_by(FileRecord.real_filename.asc())
        elif sort_by == 'date_asc': files_q = files_q.order_by(FileRecord.upload_time.asc())
        else: files_q = files_q.order_by(FileRecord.upload_time.desc())
        
        folders = folders_q.all()
        files = files_q.all()

    return render_template('index.html', files=files, folders=folders, current_folder=current_folder, breadcrumbs=breadcrumbs, query=query, sort_by=sort_by)

@app.route('/create_folder', methods=['POST'])
def create_folder():
    folder_name = request.form.get('folder_name')
    parent_id = request.form.get('parent_id', type=int)
    if folder_name:
        # 🌟 自动检查并重命名防冲突
        safe_folder_name = get_unique_foldername(parent_id, folder_name)
        
        db.session.add(Folder(name=safe_folder_name, parent_id=parent_id))
        db.session.commit()
        
        if safe_folder_name != folder_name:
            flash(f'发现同名文件夹，已自动重命名为 "{safe_folder_name}"', 'info')
        else:
            flash(f'文件夹 "{folder_name}" 创建成功', 'success')
            
    return redirect(url_for('index', folder_id=parent_id) if parent_id else url_for('index'))

@app.route('/upload', methods=['POST'])
def upload_file():
    uploaded_files = request.files.getlist('file')
    relative_paths = request.form.getlist('paths') 
    current_view_folder_id = request.form.get('folder_id', type=int)
    
    success_count = 0
    rename_count = 0
    
    for i, file in enumerate(uploaded_files):
        if file and file.filename:
            # 1. 保存物理文件 (使用乱码UUID，绝对安全)
            original_name = file.filename
            ext = os.path.splitext(original_name)[1]
            safe_name = str(uuid.uuid4()) + ext
            save_path = os.path.join(app.config['UPLOAD_FOLDER'], safe_name)
            file.save(save_path)
            
            # 2. 计算文件的归属文件夹
            target_folder_id = current_view_folder_id
            if i < len(relative_paths) and relative_paths[i]:
                path_parts = relative_paths[i].split('/')
                if len(path_parts) > 1:
                    folders_to_create = path_parts[:-1] 
                    temp_parent_id = current_view_folder_id
                    for folder_name in folders_to_create:
                        # 拖拽文件夹上传时，同名文件夹自动合并
                        temp_parent_id = get_or_create_subfolder(temp_parent_id, folder_name)
                    target_folder_id = temp_parent_id

            # 🌟 3. 获取安全的、不冲突的显示名称
            display_name = get_unique_filename(target_folder_id, original_name)
            if display_name != original_name:
                rename_count += 1

            # 4. 存入数据库
            db.session.add(FileRecord(real_filename=display_name, disk_filename=safe_name, filepath=save_path, folder_id=target_folder_id))
            success_count += 1
            
    if success_count > 0:
        db.session.commit()
        if rename_count > 0:
            flash(f'成功上传 {success_count} 个文件 (其中 {rename_count} 个同名文件已自动加后缀)', 'success')
        else:
            flash(f'成功上传 {success_count} 个文件！', 'success')
            
    return redirect(url_for('index', folder_id=current_view_folder_id) if current_view_folder_id else url_for('index'))

@app.route('/move_file', methods=['POST'])
def move_file():
    data = request.json
    f = FileRecord.query.get(data.get('file_id'))
    if f:
        tid = data.get('folder_id')
        target_folder_id = int(tid) if tid != 'root' else None
        
        # 🌟 移动文件时如果目标文件夹有同名文件，自动重命名
        f.real_filename = get_unique_filename(target_folder_id, f.real_filename)
        f.folder_id = target_folder_id
        db.session.commit()
        return jsonify({'status': 'success'})
    return jsonify({'status': 'error'}), 400

@app.route('/view/<int:file_id>')
def view_file(file_id):
    f = FileRecord.query.get_or_404(file_id)
    return send_from_directory(app.config['UPLOAD_FOLDER'], f.disk_filename, as_attachment=False, download_name=f.real_filename)

@app.route('/download/<int:file_id>')
def download_file(file_id):
    f = FileRecord.query.get_or_404(file_id)
    return send_from_directory(app.config['UPLOAD_FOLDER'], f.disk_filename, as_attachment=True, download_name=f.real_filename)

@app.route('/delete/<int:file_id>', methods=['POST'])
def delete_file(file_id):
    f = FileRecord.query.get_or_404(file_id)
    fid = f.folder_id
    try:
        if os.path.exists(f.filepath): os.remove(f.filepath)
    except: pass
    db.session.delete(f)
    db.session.commit()
    flash('文件已删除', 'warning')
    return redirect(url_for('index', folder_id=fid) if fid else url_for('index'))

@app.route('/delete_folder/<int:folder_id>', methods=['POST'])
def delete_folder(folder_id):
    folder = Folder.query.get_or_404(folder_id)
    parent_id = folder.parent_id
    mode = request.form.get('mode', 'keep') 

    if mode == 'delete':
        delete_folder_contents_recursive(folder)
        db.session.delete(folder)
        flash(f'文件夹 "{folder.name}" 及其所有内容已彻底删除。', 'danger')
    else:
        # 🌟 保留内容移到上一级时，如果上一级有同名文件/文件夹，自动重命名防冲突
        for f in FileRecord.query.filter_by(folder_id=folder_id).all(): 
            f.real_filename = get_unique_filename(parent_id, f.real_filename)
            f.folder_id = parent_id
        for sub in Folder.query.filter_by(parent_id=folder_id).all(): 
            sub.name = get_unique_foldername(parent_id, sub.name)
            sub.parent_id = parent_id
        db.session.delete(folder)
        flash(f'文件夹 "{folder.name}" 已删除，内容已安全移至上级目录。', 'info')

    db.session.commit()
    return redirect(url_for('index', folder_id=parent_id) if parent_id else url_for('index'))

@app.route('/batch_delete', methods=['POST'])
def batch_delete():
    data = request.json
    file_ids = data.get('file_ids', [])
    folder_ids = data.get('folder_ids', [])
    
    for fid in file_ids:
        f = FileRecord.query.get(fid)
        if f:
            try: os.remove(f.filepath)
            except: pass
            db.session.delete(f)
            
    for fold_id in folder_ids:
        fold = Folder.query.get(fold_id)
        if fold:
            delete_folder_contents_recursive(fold)
            db.session.delete(fold)
            
    db.session.commit()
    return jsonify({'status': 'success'})

if __name__ == '__main__':
    with app.app_context():
        db.create_all()
    app.run(host='0.0.0.0', port=5001, debug=True)