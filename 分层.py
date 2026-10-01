"""
C4D 分层渲染与深度参数提取脚本 (修复多通道与距离计算)
====================================
1. 在工程目录生成 "分层_时间戳" 文件夹
2. 按照 MODEL_NAMES 列表处理模型
3. 按实际渲染相机计算模型的最近/最远相机空间深度及量化宽度
4. 以 2x SSAA 渲染并按通道专用规则降采样，输出透明 RGBA/Normal/Depth PNG
5. 生成 camera_config.txt 参数文件
"""
import c4d
import os
import math
import struct
import zlib
from datetime import datetime
from c4d import documents, bitmaps

# ================= 用户配置 =================
# 需要处理的模型列表（根据实际情况修改）
MODEL_NAMES = ["四面体", "底盘"] 

# 输出的分辨率
RESOLUTION = 466

# SSAA 倍率：2 表示内部以 932x932 渲染，最终仍输出 466x466
SSAA_SCALE = 2

# 渲染相机名称
CAMERA_NAME = "Camera_Ortho"

# ===========================================

DEPTH_DIVISOR = 254.0
DEPTH_FILL_VALUE = 255
RDATA_CAMERA_ID = 1013
# ---------- 通用工具函数 ----------
def _out_dir():
    doc = documents.GetActiveDocument()
    base = doc.GetDocumentPath() if (doc and doc.GetDocumentPath()) else os.getcwd()
    d = os.path.join(base, "分层_" + datetime.now().strftime("%Y%m%d_%H%M%S"))
    if not os.path.exists(d):
        os.makedirs(d)
    return d


def _png_chunk(kind, data):
    return (struct.pack(">I", len(data)) + kind + data
            + struct.pack(">I", zlib.crc32(kind + data) & 0xffffffff))


def _write_rgba_png(path, width, height, rows):
    """写入无色彩管理干预的 8-bit RGBA PNG；rows 中每行恰为 width*4 字节。"""
    raw = b"".join(b"\x00" + bytes(row) for row in rows)
    payload = (b"\x89PNG\r\n\x1a\n"
               + _png_chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
               + _png_chunk(b"IDAT", zlib.compress(raw, 9))
               + _png_chunk(b"IEND", b""))
    with open(path, "wb") as handle:
        handle.write(payload)


def _safe_set(container, key_name, value):
    key = getattr(c4d, key_name, None)
    if key is None:
        return False
    try:
        container[key] = value
        return True
    except Exception:
        return False


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
    for op in _iter_all(doc.GetFirstObject()):
        if isinstance(op, c4d.CameraObject):
            return op
    return None


def _bind_render_camera(doc, rd, cam):
    for name in ("RDATA_CAMERA", "RDATA_RENDERCAMERA", "RDATA_SCENECAMERA"):
        key = getattr(c4d, name, None)
        if key is not None:
            try:
                rd[key] = cam
                return True
            except Exception:
                pass
    try:
        rd[RDATA_CAMERA_ID] = cam
        return True
    except Exception:
        pass
    try:
        bd = doc.GetRenderBaseDraw()
        if bd is not None:
            bd.SetSceneCamera(cam)
            return True
    except Exception:
        pass
    return False


def _iter_descendants(root):
    """仅遍历 root 及其后代；绝不越过 root 去遍历同级对象。"""
    stack = [root]
    while stack:
        op = stack.pop()
        yield op
        children = []
        child = op.GetDown()
        while child is not None:
            children.append(child)
            child = child.GetNext()
        stack.extend(reversed(children))


def _iter_world_bbox_corners(root):
    """逐对象变换其局部包围盒角点，避免先做世界 AABB 再投影造成的虚高。"""
    for op in _iter_descendants(root):
        rad = op.GetRad()
        if rad.GetLength() <= 0.0:
            continue
        mp, mg = op.GetMp(), op.GetMg()
        for sx in (-1.0, 1.0):
            for sy in (-1.0, 1.0):
                for sz in (-1.0, 1.0):
                    yield mg * (mp + c4d.Vector(sx * rad.x, sy * rad.y, sz * rad.z))


def _camera_depth_range(model, camera):
    """返回模型相对于实际渲染相机的 (near, far)。单位为场景 cm。

    在 C4D 的对象矩阵中，v3 是此场景相机的前向轴；故前方点的深度为
    (point - camera_position) dot camera_forward_axis。绝不能取 abs()，否则相机后方
    的几何也会伪装成前方的有效深度。
    """
    cam_mg = camera.GetMg()
    forward_axis = cam_mg.v3.GetNormalized()
    if forward_axis.GetLength() <= 1e-12:
        raise RuntimeError("相机方向退化，无法计算深度")
    depths = [(point - cam_mg.off).Dot(forward_axis)
              for point in _iter_world_bbox_corners(model)]
    depths = [depth for depth in depths if depth > 0.0]
    if not depths:
        raise RuntimeError("模型不在相机前方，无法计算有效深度")
    return min(depths), max(depths)


def _get_normal_pass_type():
    """不同 C4D 版本对 Material Normal 的常量名称略有差异。"""
    for name in ("VPBUFFER_MAT_NORMAL", "VPBUFFER_NORMAL",
                 "VPBUFFER_MATERIAL_NORMAL", "VPBUFFER_MATERIALNORMAL"):
        value = getattr(c4d, name, None)
        if value is not None:
            return value, name
    raise RuntimeError("此 C4D 版本未导出 Material Normal 多通道常量")


def _ensure_multipass(render_data, pass_type, label):
    """确保渲染设置的多通道列表包含指定通道，重复运行保持幂等。"""
    multipass = render_data.GetFirstMultipass()
    while multipass is not None:
        if multipass.GetDataInstance()[c4d.MULTIPASSOBJECT_TYPE] == pass_type:
            return
        multipass = multipass.GetNext()
    multipass = c4d.BaseList2D(c4d.Zmultipass)
    if multipass is None:
        raise MemoryError("无法创建 %s 多通道" % label)
    multipass.GetDataInstance()[c4d.MULTIPASSOBJECT_TYPE] = pass_type
    render_data.InsertMultipass(multipass)
    print("  [多通道] 已添加: %s" % label)


def _ensure_normal_post_effect(render_data, rd):
    """添加 Normal Pass 后期效果，提供图像查看器中的平滑法线层。"""
    post = render_data.GetFirstVideoPost()
    while post is not None:
        if post.GetType() == c4d.VPnormalpass:
            break
        post = post.GetNext()
    if post is None:
        post = documents.BaseVideoPost(c4d.VPnormalpass)
        if post is None:
            raise RuntimeError("无法创建 C4D Normal Pass 后期效果")
        render_data.InsertVideoPostLast(post)
        print("  [后期效果] 已添加 Normal Pass")
    post[c4d.VP_NORMALPASS_SPACE] = c4d.VP_NORMALPASS_SPACE_CAMERA
    post[c4d.VP_NORMALPASS_FLIP] = c4d.VP_NORMALPASS_FLIP_RGB
    post[c4d.VP_NORMALPASS_INVERTZ] = False
    rd[c4d.RDATA_POSTEFFECTS_ENABLE] = True


def _find_smooth_normal_layer(bmp):
    """按图层名称选择平滑/着色法线，避免使用黑色的材质法线。"""
    names = []
    for layer in bmp.GetLayers():
        name = str(layer.GetParameter(c4d.MPBTYPE_NAME) or "")
        names.append(name)
        key = name.lower().replace(" ", "")
        if (("法线" in name and "平滑" in name)
                or ("normal" in key and ("smooth" in key or "shading" in key))):
            return layer
    raise RuntimeError("未找到‘法线（平滑）’层；可用图层: %s" % ", ".join(names))


def _find_depth_layer(bmp):
    """优先选择渲染器实际产生的 Z-Depth AOV，而不是恒为零的 C4D 包装层。"""
    candidates = []
    names = []
    for layer in bmp.GetLayers():
        name = str(layer.GetParameter(c4d.MPBTYPE_NAME) or "")
        names.append(name)
        key = name.lower().replace(" ", "").replace("_", "-")
        if ("z-深度" in key or "z深度" in key
                or "z-depth" in key or "zdepth" in key):
            return layer, name
        if "深度" in name or "depth" in key:
            candidates.append((layer, name))
    if candidates:
        return candidates[0]
    raise RuntimeError("未找到 Z-Depth 层；可用图层: %s" % ", ".join(names))


def _configure_multipass(render_data, rd, out_dir, model_name):
    """配置真正的 C4D 多通道保存，不使用不存在的 RDATA_MULTIPATH 字段。"""
    normal_type, normal_name = _get_normal_pass_type()
    _ensure_multipass(render_data, c4d.VPBUFFER_DEPTH, "Depth")
    _ensure_multipass(render_data, normal_type, normal_name)
    _ensure_multipass(render_data, c4d.VPBUFFER_POSTEFFECT, "Post Effects")
    _ensure_normal_post_effect(render_data, rd)

    # 仅启用多通道供内存中的 MultipassBitmap 读取；文件由后续逻辑显式写成 PNG。
    rd[c4d.RDATA_MULTIPASS_ENABLE] = True
    rd[c4d.RDATA_MULTIPASS_SAVEIMAGE] = False
    rd[c4d.RDATA_MULTIPASS_STRAIGHTALPHA] = True
    print("  [多通道] 已启用内存输出（最终文件为透明 PNG）")


def _set_all_models_visibility(doc, visible_model_name, all_model_names):
    for name in all_model_names:
        op = doc.SearchObject(name)
        if op:
            op.SetRenderMode(c4d.MODE_ON if name == visible_model_name else c4d.MODE_OFF)
    c4d.EventAdd()


def _render_and_save(doc, render_data, rd, model_name, out_dir, camera, near_dist, far_dist):
    render_resolution = RESOLUTION * SSAA_SCALE
    # 基础渲染参数
    _safe_set(rd, "RDATA_XRES", float(render_resolution))
    _safe_set(rd, "RDATA_YRES", float(render_resolution))
    _safe_set(rd, "RDATA_XRES_VIRTUAL", float(render_resolution))
    _safe_set(rd, "RDATA_YRES_VIRTUAL", float(render_resolution))
    _safe_set(rd, "RDATA_FILMASPECT", 1.0)
    _safe_set(rd, "RDATA_PIXELASPECT", 1.0)
    _safe_set(rd, "RDATA_FORMAT", int(c4d.FILTER_PNG))
    _safe_set(rd, "RDATA_ALPHACHANNEL", True)
    _safe_set(rd, "RDATA_STRAIGHTALPHA", True)
    _safe_set(rd, "RDATA_SAVEIMAGE", True)
    _configure_multipass(render_data, rd, out_dir, model_name)
    render_data.Message(c4d.MSG_UPDATE)
    c4d.EventAdd()

    _bind_render_camera(doc, rd, camera)

    main_path = os.path.join(out_dir, "%s_RGBA.png" % model_name)
    rd[c4d.RDATA_PATH] = main_path

    # RenderDocument 只保证把结果写入传入的位图，不能依赖 RDATA_* 自动落盘。
    # 用浮点 MultipassBitmap 承接后再按通道 ID 显式保存，Depth 不会被压成 8-bit。
    bmp = bitmaps.MultipassBitmap(render_resolution, render_resolution, c4d.COLORMODE_RGBf)
    if bmp is None:
        print("  ✗ 多通道 bitmap 初始化失败")
        return False

    flags = c4d.RENDERFLAGS_EXTERNAL | c4d.RENDERFLAGS_SHOWERRORS
    res = documents.RenderDocument(doc, rd, bmp, flags)
    
    if res != c4d.RENDERRESULT_OK:
        print("  ✗ 渲染失败")
        return False
    print("  [SSAA] 内部=%dx%d，输出=%dx%d，倍率=%dx"
          % (render_resolution, render_resolution, RESOLUTION, RESOLUTION, SSAA_SCALE))

    depth_layer, depth_layer_name = _find_depth_layer(bmp)
    normal_layer = _find_smooth_normal_layer(bmp)
    print("  [Depth 图层] %s" % depth_layer_name)

    alpha_layer = bmp.GetInternalChannel()
    alpha_rows = []
    for py in range(render_resolution):
        row = []
        for px in range(render_resolution):
            if alpha_layer is not None:
                alpha = bmp.GetAlphaPixel(alpha_layer, px, py)
                row.append(max(0, min(255, int(alpha))))
            else:
                row.append(255)
        alpha_rows.append(row)

    def alpha_at(x, y):
        return alpha_rows[y][x]

    # C4D 深度层在不同渲染器中可能是实际距离、0..1 深度或已归一化颜色。
    # 先根据 Alpha 有效像素测出其原始范围，再统一映射到本层 near/far。
    # GetPixel/GetPixelDirect 都会经过 0..255 的显示转换，窄深度范围只剩少量整数级。
    # GetPixelCnt + RGBf 直接取得 32-bit 浮点数据，保留连续的 Z-Depth 精度。
    float_depth_rows = []
    float_read_mode = "RGBf"
    for py in range(render_resolution):
        # 本机返回的图层色彩模式为 36，即 RGBf。C4D 的 Python 绑定在成功时
        # 可能返回 None，官方示例也不检查返回值，因此只捕获真正的异常。
        rgb_buffer = bytearray(render_resolution * 12)
        rgb_view = memoryview(rgb_buffer)
        try:
            depth_layer.GetPixelCnt(
                0, py, render_resolution, rgb_view, 12,
                c4d.COLORMODE_RGBf, c4d.PIXELCNT_0)
        except Exception as exc:
            color_mode = depth_layer.GetParameter(c4d.MPBTYPE_COLORMODE)
            raise RuntimeError("无法读取 Z-Depth 浮点像素（第 %d 行，图层色彩模式=%s）: %s"
                               % (py, color_mode, exc))
        rgb_values = struct.unpack("=%df" % (render_resolution * 3), rgb_buffer)
        float_depth_rows.append(tuple(rgb_values[i * 3] for i in range(render_resolution)))

    # 只使用完全覆盖的内部像素估计浮点 AOV 范围，排除透明背景与 AA 边缘混入的 0。
    valid_depths = []
    for py in range(render_resolution):
        for px in range(render_resolution):
            value = float(float_depth_rows[py][px])
            if alpha_at(px, py) >= 250 and math.isfinite(value) and value > 1e-12:
                valid_depths.append(value)
    if not valid_depths:
        raise RuntimeError("Z-Depth 浮点层没有有效模型像素")
    valid_depths.sort()
    # 去掉极少量异常值，防止一个坏像素破坏整层量化区间。
    trim = max(0, min(len(valid_depths) // 1000, 32))
    raw_min = valid_depths[trim]
    raw_max = valid_depths[-1 - trim]
    if not math.isfinite(raw_min) or not math.isfinite(raw_max):
        raise RuntimeError("Depth 层没有有效像素")
    if raw_max - raw_min <= 1e-12:
        raise RuntimeError("Depth 图层 '%s' 的像素全部为 %.6f；请在 OC AOV 中启用 Z-Depth"
                           % (depth_layer_name, raw_min))
    print("  [Depth 浮点范围/%s] %.9f .. %.9f"
          % (float_read_mode, raw_min, raw_max))

    rgba_rows, normal_rows, depth_rows = [], [], []
    depth_span = far_dist - near_dist
    if depth_span <= 1e-12:
        depth_span = 1.0
    depth_step = depth_span / DEPTH_DIVISOR
    sample_count = SSAA_SCALE * SSAA_SCALE

    def to_byte(value):
        return max(0, min(255, int(round(value))))

    for y in range(RESOLUTION):
        rgba_row, normal_row, depth_row = bytearray(), bytearray(), bytearray()
        for x in range(RESOLUTION):
            rgba_a_sum = rgba_r_sum = rgba_g_sum = rgba_b_sum = 0.0
            normal_x_sum = normal_y_sum = normal_z_sum = normal_weight = 0.0
            depth_samples = []
            depth_alpha_sum = 0.0

            for oy in range(SSAA_SCALE):
                sy = y * SSAA_SCALE + oy
                for ox in range(SSAA_SCALE):
                    sx = x * SSAA_SCALE + ox
                    alpha = alpha_at(sx, sy)
                    weight = alpha / 255.0

                    r, g, b = bmp.GetPixel(sx, sy)
                    rgba_a_sum += alpha
                    rgba_r_sum += float(r) * weight
                    rgba_g_sum += float(g) * weight
                    rgba_b_sum += float(b) * weight

                    if alpha > 0:
                        nr, ng, nb = normal_layer.GetPixel(sx, sy)
                        normal_x_sum += (float(nr) / 127.5 - 1.0) * weight
                        normal_y_sum += (float(ng) / 127.5 - 1.0) * weight
                        normal_z_sum += (float(nb) / 127.5 - 1.0) * weight
                        normal_weight += weight

                    raw_depth = float(float_depth_rows[sy][sx])
                    if alpha > 0 and math.isfinite(raw_depth) and raw_depth > 1e-12:
                        unit_depth = max(0.0, min(1.0,
                            (raw_depth - raw_min) / (raw_max - raw_min)))
                        physical_depth = near_dist + unit_depth * depth_span
                        depth_samples.append((physical_depth, weight))
                        depth_alpha_sum += alpha

            # RGBA：预乘 Alpha 平均，防止透明轮廓出现黑边/白边。
            rgba_alpha = to_byte(rgba_a_sum / sample_count)
            rgba_weight = rgba_a_sum / 255.0
            if rgba_weight > 1e-12:
                out_r = to_byte(rgba_r_sum / rgba_weight)
                out_g = to_byte(rgba_g_sum / rgba_weight)
                out_b = to_byte(rgba_b_sum / rgba_weight)
            else:
                out_r = out_g = out_b = 0

            # Normal：在向量空间做 Alpha 加权平均，再重新归一化。
            normal_alpha = rgba_alpha
            normal_length = math.sqrt(normal_x_sum * normal_x_sum
                                      + normal_y_sum * normal_y_sum
                                      + normal_z_sum * normal_z_sum)
            if normal_weight > 1e-12 and normal_length > 1e-12:
                nx = normal_x_sum / normal_length
                ny = normal_y_sum / normal_length
                nz = normal_z_sum / normal_length
                out_nr = to_byte((nx + 1.0) * 127.5)
                out_ng = to_byte((ny + 1.0) * 127.5)
                out_nb = to_byte((nz + 1.0) * 127.5)
            else:
                out_nr = out_ng = out_nb = 0
                normal_alpha = 0

            # Depth：连续表面取 Alpha 加权平均；遇到明显深度断层则取最近表面，
            # 避免在遮挡边界生成不存在的中间深度。背景不参与深度计算。
            if depth_samples:
                depths = [item[0] for item in depth_samples]
                if max(depths) - min(depths) <= depth_step * 2.0:
                    weight_sum = sum(item[1] for item in depth_samples)
                    if weight_sum > 1e-12:
                        resolved_depth = sum(d * w for d, w in depth_samples) / weight_sum
                    else:
                        resolved_depth = min(depths)
                else:
                    resolved_depth = min(depths)
                q = max(0, min(254, int(round((resolved_depth - near_dist) / depth_step))))
                gray = 255 - int(round(q * 255.0 / 254.0))
                depth_alpha = to_byte(depth_alpha_sum / sample_count)
            else:
                gray = 255
                depth_alpha = 0

            rgba_row.extend((out_r, out_g, out_b, rgba_alpha))
            normal_row.extend((out_nr, out_ng, out_nb, normal_alpha))
            depth_row.extend((gray, gray, gray, depth_alpha))
        rgba_rows.append(rgba_row)
        normal_rows.append(normal_row)
        depth_rows.append(depth_row)

    normal_path = os.path.join(out_dir, "%s_Normal.png" % model_name)
    depth_path = os.path.join(out_dir, "%s_Depth.png" % model_name)
    _write_rgba_png(main_path, RESOLUTION, RESOLUTION, rgba_rows)
    _write_rgba_png(normal_path, RESOLUTION, RESOLUTION, normal_rows)
    _write_rgba_png(depth_path, RESOLUTION, RESOLUTION, depth_rows)
    print("  ✓ 已保存透明 PNG: %s, %s, %s" % (main_path, normal_path, depth_path))
    return True


def _write_params_txt(out_dir, offsets, scales, fills):
    path = os.path.join(out_dir, "camera_config.txt")
    content = ("# Depth PNG convention: q=round((depth-near)/scale), near=0, far=254.\n"
               "# PNG gray is inverted for preview: near=white, far=black; alpha=0 is background.\n\n"
               "[depthAddOffsets]\n")
    for val in offsets: content += "%.16f\n" % val
    content += "\n[depthScaleFactors]\n"
    for val in scales: content += "%.16f\n" % val
    content += "\n# 255 is the fixed background code; valid depth codes are 0..254.\n"
    content += "[depthFillValues]\n"
    for val in fills: content += "%d\n" % val
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        print("[main] 参数文件已写入: %s" % path)
    except Exception as e:
        print("[error] 写入参数文件失败: %s" % e)


# ---------- 主逻辑 ----------
def main():
    doc = documents.GetActiveDocument()
    if doc is None:
        raise RuntimeError("没有活动文档")

    cam = _find_camera(doc, CAMERA_NAME)
    if cam is None:
        raise RuntimeError("找不到相机，请检查 CAMERA_NAME 设置")

    OUT = _out_dir()
    print("[main] 输出目录: %s" % OUT)

    rdata_obj = doc.GetActiveRenderData()
    rd = rdata_obj.GetDataInstance()
    original_xres = rd[c4d.RDATA_XRES]
    original_yres = rd[c4d.RDATA_YRES]
    original_xres_virtual = rd[c4d.RDATA_XRES_VIRTUAL]
    original_yres_virtual = rd[c4d.RDATA_YRES_VIRTUAL]
    original_modes = {}
    for name in MODEL_NAMES:
        op = doc.SearchObject(name)
        if op is not None:
            original_modes[name] = op.GetRenderMode()

    depth_add_offsets = []
    depth_scale_factors = []
    depth_fill_values = []

    try:
        for model_name in MODEL_NAMES:
            model = doc.SearchObject(model_name)
            if model is None:
                print("[警告] 找不到模型: %s，跳过" % model_name)
                continue

            print("\n---- 正在处理模型: %s ----" % model_name)

            try:
                min_dist, max_dist = _camera_depth_range(model, cam)
            except RuntimeError as exc:
                print("  [警告] %s，跳过" % exc)
                continue

            width = (max_dist - min_dist) / DEPTH_DIVISOR
            if width <= 0.0:
                raise RuntimeError("模型 %s 的深度范围退化" % model_name)

            print("  [实际渲染相机] 最远: %.6f, 最近: %.6f" % (max_dist, min_dist))
            print("  宽度(ScaleFactor): %.6f" % width)

            # 与 PNG 的 q 一致：depth = near + q * scale，q 的有效范围为 0..254。
            depth_add_offsets.append(min_dist)
            depth_scale_factors.append(width)
            depth_fill_values.append(DEPTH_FILL_VALUE)

            _set_all_models_visibility(doc, model_name, MODEL_NAMES)
            _render_and_save(doc, rdata_obj, rd, model_name, OUT, cam, min_dist, max_dist)
    finally:
        rd[c4d.RDATA_XRES] = original_xres
        rd[c4d.RDATA_YRES] = original_yres
        rd[c4d.RDATA_XRES_VIRTUAL] = original_xres_virtual
        rd[c4d.RDATA_YRES_VIRTUAL] = original_yres_virtual
        rdata_obj.Message(c4d.MSG_UPDATE)
        for name, mode in original_modes.items():
            op = doc.SearchObject(name)
            if op is not None:
                op.SetRenderMode(mode)
        c4d.EventAdd()
        print("[main] 已恢复原渲染分辨率与对象可见性")

    _write_params_txt(OUT, depth_add_offsets, depth_scale_factors, depth_fill_values)
    print("\n=== 处理完成 ===")

if __name__ == '__main__':
    main()
