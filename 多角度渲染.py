"""
手表多角度渲染 —— C4D 2026 版（兼容多版本 · 修复 RDATA_CAMERA 缺失）
============================================================
1. 自动按名称查找相机（不用手动选中）
2. 相机绕模型公转，模型不动，输出 9 张透明底 PNG
3. 旋转顺序：先绕世界 X 轴转 pitch，再绕倾斜后的 Z 轴转 yaw
4. 在输出目录生成 params.txt，记录：
   - viewer_distance_cm    : 相机到表盘中心距离（cm）
   - real_dial_diameter_cm : 模型实际尺寸（cm，与 virtual_dial_diameter 相同）
   - virtual_dial_diameter : 模型在软件中的尺寸（包围盒最长边）
   - fov_deg               : 相机视野角（度）

关键修复：
  ★ C4D 2026 的 c4d 命名空间里没有 RDATA_CAMERA 常量
    → 改用数值 ID 1013 + GetRenderBaseDraw().SetSceneCamera() 双保险
  ★ 清除相机 Target，避免 SetMg 的旋转被 C4D 覆盖
  ★ 打印场景中所有相机 + 投影模式，便于排查
"""
import c4d
import os
import math
from datetime import datetime
from c4d import documents, bitmaps

# ================= 用户配置 =================
OUTPUT_DIR   = ""
RESOLUTION   = 466
FILE_PREFIX  = "image"

# ★ 相机名称（想用透视效果就改成 "Camera"，正交就保持 "Camera_Ortho"）
CAMERA_NAME  = "Camera_per"
MODEL_NAME   = "All"

WORLD_UP = (0.0, 0.0, 1.0)
FOCUS    = (0.0, 0.0, 0.0)

ANGLES = [(0, 0), (5, 5), (5, -5), (-5, 5), (-5, -5),
          (30, 30), (30, -30), (-30, 30), (-30, -30)]
# ===========================================

# ★ C4D SDK 中 RDATA_CAMERA 的参数 ID（即使 Python 没导出常量也能用）
RDATA_CAMERA_ID = 1013


# ---------- 通用工具函数 ----------
def vec3(t):
    if isinstance(t, c4d.Vector):
        return t
    if isinstance(t, (tuple, list)):
        if len(t) < 3:
            raise ValueError("vec3 需要至少 3 个分量，收到: %r" % (t,))
        return c4d.Vector(float(t[0]), float(t[1]), float(t[2]))
    raise TypeError("vec3 不支持的类型: %r" % type(t))


def _safe_set(container, key_name, value):
    key = getattr(c4d, key_name, None)
    if key is None:
        return False
    try:
        container[key] = value
        return True
    except Exception as e:
        print("  [跳过] %s = %r -> %s" % (key_name, value, e))
        return False


def _out_dir():
    if OUTPUT_DIR:
        d = OUTPUT_DIR
    else:
        doc = documents.GetActiveDocument()
        base = doc.GetDocumentPath() if (doc and doc.GetDocumentPath()) else os.getcwd()
        d = os.path.join(base, "多角度_" + datetime.now().strftime("%Y%m%d_%H%M%S"))
    if not os.path.exists(d):
        os.makedirs(d)
    return d


def _iter_all(root):
    stack = [root]
    while stack:
        op = stack.pop()
        while op:
            yield op
            if op.GetDown():
                stack.append(op.GetDown())
            op = op.GetNext()


def _find_camera(doc, name):
    if name:
        obj = doc.SearchObject(name)
        if isinstance(obj, c4d.CameraObject):
            return obj
        print("  [警告] 未找到名为 %r 的相机，尝试自动查找第一个相机" % name)
    for op in _iter_all(doc.GetFirstObject()):
        if isinstance(op, c4d.CameraObject):
            print("  [自动] 使用相机: %s" % op.GetName())
            return op
    return None


def _find_model(doc, camera, name):
    if name:
        obj = doc.SearchObject(name)
        if obj is not None:
            return obj
        print("  [警告] 未找到名为 %r 的模型，尝试自动查找" % name)
    keywords = ("model", "模型", "watch", "手表")
    skip = {c4d.Ocamera, c4d.Olight, c4d.Osky, c4d.Obackground}
    fallback = None
    for op in _iter_all(doc.GetFirstObject()):
        if op == camera or op.GetType() in skip:
            continue
        nm = (op.GetName() or "").lower()
        if any(k in nm for k in keywords):
            return op
        if fallback is None and isinstance(op, c4d.PointObject):
            fallback = op
    return fallback


def _list_cameras(doc):
    print("[diag] 场景中的相机：")
    proj_names = {0: "透视", 1: "正交", 2: "正面", 3: "球面"}
    n = 0
    for op in _iter_all(doc.GetFirstObject()):
        if isinstance(op, c4d.CameraObject):
            n += 1
            proj = None
            try:
                proj = op[c4d.CAMERAOBJECT_PROJECTION]
            except Exception:
                pass
            proj_txt = proj_names.get(proj, "未知")
            print("    - %s  (投影=%r -> %s)"
                  % (op.GetName(), proj, proj_txt))
    if n == 0:
        print("    (场景中没有相机)")


def _clear_camera_target(cam):
    for key_name, val in (("CAMERAOBJECT_TARGETOBJECT", None),
                          ("CAMERAOBJECT_TARGETMODE", 0)):
        key = getattr(c4d, key_name, None)
        if key is None:
            continue
        try:
            cam[key] = val
            print("  [清理] 已清除相机属性 %s" % key_name)
        except Exception as e:
            print("  [跳过] %s -> %s" % (key_name, e))

    tag = cam.GetFirstTag()
    while tag is not None:
        nxt = tag.GetNext()
        if tag.GetType() == getattr(c4d, "Ttargetexpression", -1):
            tag.Remove()
            print("  [清理] 已移除 Target 标签")
        tag = nxt


# ---------- ★ 多版本兼容地绑定渲染相机 ----------
def _bind_render_camera(doc, rd, cam):
    """
    把 cam 绑定为渲染相机。兼容不同 C4D 版本：
      1) 若 c4d 命名空间里有 RDATA_CAMERA 常量，直接用它
      2) 否则用数值 ID 1013（C4D SDK 中 RDATA_CAMERA 的固定 ID）
      3) 再不行用 GetRenderBaseDraw().SetSceneCamera()
      4) 最后用 SetActiveObject()
    """
    # 尝试 1：优先尝试 c4d 命名空间里的已知常量名（兼容老版本）
    for name in ("RDATA_CAMERA", "RDATA_RENDERCAMERA", "RDATA_SCENECAMERA"):
        key = getattr(c4d, name, None)
        if key is None:
            continue
        try:
            rd[key] = cam
            print("[main] 已通过常量 %s 绑定渲染相机 -> %s"
                  % (name, cam.GetName()))
            c4d.EventAdd()
            return True
        except Exception as e:
            print("  [跳过] %s -> %s" % (name, e))

    # 尝试 2：用数值 ID（RDATA_CAMERA = 1013）
    try:
        rd[RDATA_CAMERA_ID] = cam
        print("[main] 已通过数值 ID %d 绑定渲染相机 -> %s"
              % (RDATA_CAMERA_ID, cam.GetName()))
        c4d.EventAdd()
        return True
    except Exception as e:
        print("  [跳过] ID %d -> %s" % (RDATA_CAMERA_ID, e))

    # 尝试 3：渲染基绘制（BaseDraw）的场景相机
    try:
        bd = doc.GetRenderBaseDraw()
        if bd is not None:
            bd.SetSceneCamera(cam)
            print("[main] 已通过 GetRenderBaseDraw().SetSceneCamera 绑定 -> %s"
                  % cam.GetName())
            c4d.EventAdd()
            return True
    except Exception as e:
        print("  [跳过] SetSceneCamera -> %s" % e)

    # 尝试 4：把相机设为活动对象
    try:
        doc.SetActiveObject(cam, c4d.SELECTION_NEW)
        print("[main] 已通过 SetActiveObject 绑定 -> %s" % cam.GetName())
        c4d.EventAdd()
        return True
    except Exception as e:
        print("  [跳过] SetActiveObject -> %s" % e)

    print("[error] 无法绑定渲染相机！")
    return False
# ---------------------------------------------------


# ---------- ★ 包围盒：返回 (min, max, found) ----------
def _bbox_minmax(obj):
    """计算 obj（含子对象）在世界坐标下的包围盒。
    返回 (mn, mx, found)。found=False 表示没有有效包围盒。"""
    doc = documents.GetActiveDocument()
    doc.SetActiveObject(obj, c4d.SELECTION_NEW)
    c4d.CallCommand(13957)
    mn = c4d.Vector( 1e18,  1e18,  1e18)
    mx = c4d.Vector(-1e18, -1e18, -1e18)
    found = False
    for op in _iter_all(obj):
        rad = op.GetRad()
        if rad.GetLength() <= 0:
            continue
        mp = op.GetMp(); mg = op.GetMg()
        found = True
        for sx in (-1, 1):
            for sy in (-1, 1):
                for sz in (-1, 1):
                    w = mg * (mp + c4d.Vector(sx*rad.x, sy*rad.y, sz*rad.z))
                    mn.x = min(mn.x, w.x); mn.y = min(mn.y, w.y); mn.z = min(mn.z, w.z)
                    mx.x = max(mx.x, w.x); mx.y = max(mx.y, w.y); mx.z = max(mx.z, w.z)
    return mn, mx, found


def _bbox_center(obj):
    mn, mx, found = _bbox_minmax(obj)
    return (mn + mx) * 0.5 if found else obj.GetMg().off


# ---------- ★ 读取相机 FOV（角度制） ----------
def _camera_fov_deg(cam):
    """从相机对象读取 FOV（弧度）→ 度。读取失败返回 None。"""
    key = getattr(c4d, "CAMERAOBJECT_FOV", None)
    if key is None:
        print("  [警告] 未找到 CAMERAOBJECT_FOV 常量，无法读取 FOV")
        return None
    try:
        fov_rad = cam[key]
        return math.degrees(float(fov_rad))
    except Exception as e:
        print("  [跳过] 读取 CAMERAOBJECT_FOV 失败: %s" % e)
        return None


# ---------- ★ 写入参数文件 ----------
def _write_params_txt(out_dir,
                      viewer_distance_cm,
                      real_dial_diameter_cm,
                      virtual_dial_diameter,
                      fov_deg):
    path = os.path.join(out_dir, "camera_config.txt")
    fov_txt = ("%.6f" % fov_deg) if (fov_deg is not None) else "N/A"
    content = (
        "# 多角度渲染参数\n"
        "viewer_distance_cm = %.6f\n"
        "real_dial_diameter_cm = %.6f\n"
        "virtual_dial_diameter = %.6f\n"
        "fov_deg = %s\n"
    ) % (viewer_distance_cm,
         real_dial_diameter_cm,
         virtual_dial_diameter,
         fov_txt)
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        print("[main] 参数文件已写入: %s" % path)
        print(content.rstrip())
    except Exception as e:
        print("[error] 写入参数文件失败: %s" % e)


def render_multiview():
    doc = documents.GetActiveDocument()
    if doc is None:
        raise RuntimeError("没有活动文档")

    # ---------- 0. 自动查找相机 & 模型 ----------
    cam = _find_camera(doc, CAMERA_NAME)
    if cam is None:
        raise RuntimeError("找不到相机，请填写正确的 CAMERA_NAME")

    _list_cameras(doc)
    _clear_camera_target(cam)
    c4d.EventAdd()

    model = _find_model(doc, cam, MODEL_NAME)
    if model is None:
        raise RuntimeError("找不到模型，请填写 MODEL_NAME")

    OUT = _out_dir()
    print("[main] 输出目录: %s" % OUT)
    print("[main] 相机: %s   模型: %s" % (cam.GetName(), model.GetName()))

    # ---------- 1. 渲染设置 ----------
    rdata_obj = doc.GetActiveRenderData()
    rd = rdata_obj.GetDataInstance()
    _safe_set(rd, "RDATA_XRES", float(RESOLUTION))
    _safe_set(rd, "RDATA_YRES", float(RESOLUTION))
    _safe_set(rd, "RDATA_FILMASPECT", 1.0)
    _safe_set(rd, "RDATA_PIXELASPECT", 1.0)
    _safe_set(rd, "RDATA_FORMAT", int(c4d.FILTER_PNG))
    _safe_set(rd, "RDATA_ALPHACHANNEL", True)
    _safe_set(rd, "RDATA_SAVEIMAGE", True)

    # ★★★ 关键修复：多版本兼容绑定渲染相机 ★★★
    if not _bind_render_camera(doc, rd, cam):
        raise RuntimeError("无法把相机绑定到渲染设置")

    # ---------- 2. 包围盒 / 中心 / 半径 / 尺寸 / FOV ----------
    mn, mx, bbox_found = _bbox_minmax(model)
    if bbox_found:
        bbox_center = (mn + mx) * 0.5
        bbox_size = mx - mn
        virtual_dial_diameter = max(bbox_size.x, bbox_size.y, bbox_size.z)
    else:
        bbox_center = model.GetMg().off
        bbox_size = c4d.Vector(0.0, 0.0, 0.0)
        virtual_dial_diameter = 0.0
        print("  [警告] 未能获取有效包围盒，virtual_dial_diameter = 0")

    center = vec3(FOCUS) if FOCUS is not None else bbox_center
    cam_mg0 = cam.GetMg()
    radius = (cam_mg0.off - center).GetLength()
    if radius <= 1e-6:
        raise RuntimeError("相机与公转中心重合")

    # 单位约定：C4D 场景 1 单位 = 1 cm
    viewer_distance_cm    = radius
    real_dial_diameter_cm = virtual_dial_diameter   # 与实际尺寸数值相同
    fov_deg               = _camera_fov_deg(cam)

    print("[main] 中心=%s  半径=%.3f" % (center, radius))
    print("[main] 包围盒 min=%s  max=%s" % (mn, mx))
    print("[main] 包围盒尺寸=%s  最长边=%.3f"
          % (bbox_size, virtual_dial_diameter))
    print("[main] viewer_distance_cm    = %.3f" % viewer_distance_cm)
    print("[main] real_dial_diameter_cm = %.3f" % real_dial_diameter_cm)
    print("[main] virtual_dial_diameter = %.3f" % virtual_dial_diameter)
    print("[main] fov_deg               = %s"
          % ("%.3f" % fov_deg if fov_deg is not None else "N/A"))

    # ---------- 2.5 写参数文件 ----------
    _write_params_txt(
        OUT,
        viewer_distance_cm,
        real_dial_diameter_cm,
        virtual_dial_diameter,
        fov_deg,
    )

    # ---------- 3. 旋转逻辑 ----------
    _up = vec3(WORLD_UP).GetNormalized()

    def pose_of(yaw_deg, pitch_deg):
        Y = math.radians(yaw_deg)
        P = math.radians(pitch_deg)

        pos = center + vec3((
            (math.cos(P) * math.sin(Y)),
            (math.cos(P) * math.cos(Y)),
            (math.sin(P))
        )) * radius

        fwd = (center - pos).GetNormalized()
        print(pos)

        z_axis = fwd
        if z_axis.GetLength() < 1e-6:
            raise RuntimeError("lookAt 退化 yaw=%s pitch=%s" % (yaw_deg, pitch_deg))

        z_axis = z_axis.GetNormalized()

        x_axis = _up.Cross(z_axis).GetNormalized()
        y_axis = z_axis.Cross(x_axis)
        y_axis = y_axis.GetNormalized()

        mw = c4d.Matrix(pos, x_axis, y_axis, z_axis)
        return mw

    # ---------- 4. 逐角度渲染 ----------
    save_data = c4d.BaseContainer()
    save_data[c4d.SAVEBIT_ALPHA] = True

    print("[main] 开始渲染 %d 个角度..." % len(ANGLES))
    ok_count = 0

    try:
        for idx, (yaw, pitch) in enumerate(ANGLES):
            print("---- [%d/%d] yaw=%d pitch=%d ----"
                  % (idx+1, len(ANGLES), yaw, pitch))

            cam.SetMg(pose_of(yaw, pitch))
            c4d.EventAdd()

            name = "%s_%d_%d" % (FILE_PREFIX, yaw, pitch)
            path = os.path.join(OUT, name + ".png")
            rd[c4d.RDATA_PATH] = path

            bmp = bitmaps.BaseBitmap()
            if not bmp.Init(RESOLUTION, RESOLUTION, 24):
                print("  ✗ bitmap 初始化失败")
                continue

            flags = c4d.RENDERFLAGS_EXTERNAL | c4d.RENDERFLAGS_SHOWERRORS
            res = documents.RenderDocument(doc, rd, bmp, flags)
            w, h = bmp.GetSize()
            print("  RenderDocument=%s  Bitmap=%dx%d" % (res, w, h))

            if res != c4d.RENDERRESULT_OK or w == 0 or h == 0:
                print("  ✗ 渲染失败")
                continue

            bmp.Save(path, c4d.FILTER_PNG, save_data)
            if os.path.exists(path):
                print("  ✓ 已保存: %d 字节" % os.path.getsize(path))
                ok_count += 1
            else:
                print("  ✗ 文件未生成")
    finally:
        cam.SetMg(cam_mg0)
        c4d.EventAdd()

    print("=== 完成: %d/%d 张 ===" % (ok_count, len(ANGLES)))
    print("=== 输出目录: %s ===" % OUT)


if __name__ == '__main__':
    render_multiview()