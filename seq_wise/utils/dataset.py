import os
import re
import struct
import random
import numpy as np
from torch.utils.data import Dataset
from scipy import signal
from scipy.fft import fft, fftshift
from dataclasses import dataclass
from pathlib import Path


POINT_TRACK_HEADER = ["点时间", "批号", "距离", "方位", "俯仰", "多普勒速度", "和幅度", "信噪比", "原始点数量"]
TRACK_HEADER = ["点时间", "批号", "滤波距离", "滤波方位", "滤波俯仰", "全速度", "X向速度", "Y向速度", "Z向速度", "航向"]

NUMERICAL_POINT_FEATURES = ["距离", "方位", "俯仰", "多普勒速度", "和幅度", "信噪比", "原始点数量"]  # 7
NUMERICAL_TRACK_FEATURES = ["滤波距离", "滤波方位", "滤波俯仰", "全速度", "X向速度", "Y向速度", "Z向速度", "航向"]  # 8
TOTAL_FEATURES_PER_TIMESTEP = len(NUMERICAL_POINT_FEATURES) + len(NUMERICAL_TRACK_FEATURES)  # 15

FS = 20e6  # 采样率 (20 MHz)
C = 3e8    # 光速 (m/s)
DELTA_R = C / (2 * FS)  # 距离分辨率


class ColPoint:
    """点迹数据列索引"""
    Time = 0       # 点时间
    TrackID = 1    # 航迹批号
    R = 2          # 距离
    AZ = 3         # 方位
    EL = 4         # 俯仰
    Doppler = 5    # 多普勒速度
    Amp = 6        # 和幅度
    SNR = 7        # 信噪比
    PointNum = 8   # 原始点数量


class ColTrack:
    """航迹数据列索引"""
    Time = 0       # 点时间
    TrackID = 1    # 航迹批号
    R = 2          # 滤波距离
    AZ = 3         # 滤波方位
    EL = 4         # 滤波俯仰
    Speed = 5      # 全速度
    Vx = 6         # X向速度(东)
    Vy = 7         # Y向速度(北)
    Vz = 8         # Z向速度(天)
    Head = 9       # 航向角


@dataclass
class BatchFile:
    """批次文件信息"""
    batch_num: int           # 航迹批号
    label: int              # 目标类型标签
    raw_file: str          # 原始回波文件路径
    point_file: str        # 点迹文件路径
    track_file: str        # 航迹文件路径


@dataclass
class Parameters:
    """雷达参数"""
    e_scan_az: float       # 方位角
    track_no_info: np.ndarray  # 航迹信息
    freq: float           # 频率
    cpi_count: int       # CPI流水号
    prt_num: int         # PRT数目
    prt: float          # PRT值
    data_length: int    # 距离维采样点数


def read_raw_data(fid):
    """
    读取解析原始回波数据
    :param fid: 文件句柄
    :return: 参数对象和数据数组
    """
    FRAME_HEAD = 0xFA55FA55
    FRAME_END = 0x55FA55FA

    # 读取帧头
    try:
        head_bytes = fid.read(4)
        if len(head_bytes) < 4:
            return None, None
        head_find = struct.unpack('<I', head_bytes)[0]  # 使用小端序
    except struct.error:
        return None, None

    # 查找帧头 - 更接近MATLAB逻辑
    file_size = os.fstat(fid.fileno()).st_size
    while head_find != FRAME_HEAD and fid.tell() < file_size:
        fid.seek(-3, 1)  # 回退3个字节
        try:
            head_bytes = fid.read(4)
            if len(head_bytes) < 4:
                return None, None
            head_find = struct.unpack('<I', head_bytes)[0]
        except struct.error:
            return None, None

    if head_find != FRAME_HEAD:
        return None, None

    # 读取帧长度
    try:
        length_bytes = fid.read(4)
        if len(length_bytes) < 4:
            return None, None
        frame_data_length = struct.unpack('<I', length_bytes)[0] * 4  # 使用小端序
    except struct.error:
        return None, None

    if frame_data_length <= 0 or frame_data_length > 1000000:
        return None, None

    # 检查帧尾 - 更接近MATLAB逻辑
    current_pos = fid.tell()

    try:
        fid.seek(current_pos + frame_data_length - 12, 0)  # 偏移到结尾
        end_bytes = fid.read(4)
        if len(end_bytes) < 4:
            return None, None
        end_find = struct.unpack('<I', end_bytes)[0]
    except (struct.error, OSError):
        return None, None

    # 验证帧头和帧尾
    while head_find != FRAME_HEAD or end_find != FRAME_END:
        fid.seek(-frame_data_length + 1, 1)  # 指针偏移

        try:
            head_bytes = fid.read(4)
            if len(head_bytes) < 4:
                return None, None
            head_find = struct.unpack('<I', head_bytes)[0]

            length_bytes = fid.read(4)
            if len(length_bytes) < 4:
                return None, None
            frame_data_length = struct.unpack('<I', length_bytes)[0] * 4

            if frame_data_length <= 0 or frame_data_length > 1000000:
                return None, None

            fid.seek(frame_data_length - 8, 1)
            end_bytes = fid.read(4)
            if len(end_bytes) < 4:
                return None, None
            end_find = struct.unpack('<I', end_bytes)[0]

        except struct.error:
            return None, None

        if fid.tell() >= file_size and (head_find != FRAME_HEAD or end_find != FRAME_END):
            print('未找到满足报文格式的数据')
            return None, None

    # 回到数据开始位置
    fid.seek(-frame_data_length + 4, 1)

    # 读取参数
    try:
        # 读取前3个uint32
        data_temp1_bytes = fid.read(12)
        if len(data_temp1_bytes) < 12:
            return None, None
        data_temp1 = np.frombuffer(data_temp1_bytes, dtype='<u4')  # 小端序uint32

        e_scan_az = data_temp1[1] * 0.01
        point_num_in_bowei = data_temp1[2]

        # 添加point_num合理性检查
        if point_num_in_bowei < 0 or point_num_in_bowei > 1000:
            print(f"点数异常: {point_num_in_bowei}")
            return None, None

        # 读取航迹信息和其他参数
        param_count = point_num_in_bowei * 4 + 5
        param_bytes = fid.read(param_count * 4)
        if len(param_bytes) < param_count * 4:
            return None, None
        data_temp = np.frombuffer(param_bytes, dtype='<u4')

        # 提取航迹信息
        if point_num_in_bowei > 0:
            track_no_info = data_temp[:point_num_in_bowei * 4]
        else:
            track_no_info = np.array([], dtype=np.uint32)

        # 提取其他参数
        base_idx = point_num_in_bowei * 4
        params = Parameters(
            e_scan_az=e_scan_az,
            track_no_info=track_no_info,
            freq=data_temp[base_idx] * 1e6,
            cpi_count=data_temp[base_idx + 1],
            prt_num=data_temp[base_idx + 2],
            prt=data_temp[base_idx + 3] * 0.0125e-6,
            data_length=data_temp[base_idx + 4]
        )


        # 参数验证
        if params.prt_num <= 0 or params.prt_num > 10000:
            print(f"PRT_num异常: {params.prt_num}")
            return None, None
        if params.prt <= 0 or params.prt > 1:
            print(f"PRT异常: {params.prt}")
            return None, None
        if params.freq <= 0 or params.freq > 1e12:
            print(f"频率异常: {params.freq}")
            return None, None

        # 读取IQ数据
        iq_data_len = params.prt_num * 31 * 2
        data_bytes = fid.read(iq_data_len * 4)
        if len(data_bytes) < iq_data_len * 4:
            print(f"IQ数据长度不足: 期望{iq_data_len * 4}, 实际{len(data_bytes)}")
            return None, None

        data_out_temp = np.frombuffer(data_bytes, dtype='<f4')  # 小端序float32

        # 重构复数数据
        data_out_real = data_out_temp[::2]
        data_out_imag = data_out_temp[1::2]
        data_out_complex = data_out_real + 1j * data_out_imag
        data_out = data_out_complex.reshape(31, params.prt_num)

        # 跳过帧尾
        fid.seek(4, 1)

        return params, data_out

    except Exception as e:
        print(f"读取数据时出错: {str(e)}")
        return None, None


def get_batch_file_list(root_dir: str):
    """
    获取批量处理文件列表
    :param root_dir: 数据根目录
    :return: 批次文件列表
    """
    iq_dir = os.path.join(root_dir, "原始回波")
    track_dir = os.path.join(root_dir, "航迹")
    point_dir = os.path.join(root_dir, "点迹")

    if not all(os.path.isdir(d) for d in [iq_dir, track_dir, point_dir]):
        raise ValueError("错误！数据根目录下需包含原始回波、点迹、航迹三个子文件夹。")

    batch_files = []
    # 遍历原始回波文件
    for raw_file in os.listdir(iq_dir):
        if not raw_file.endswith('.dat'):
            continue

        # 解析文件名
        match = re.match(r'^(\d+)_Label_(\d+)\.dat$', raw_file)
        if not match:
            continue

        batch_num = int(match.group(1))
        label = int(match.group(2))

        # 查找对应的点迹和航迹文件
        point_pattern = f'PointTracks_{batch_num}_{label}_*.txt'
        track_pattern = f'Tracks_{batch_num}_{label}_*.txt'

        point_files = list(Path(point_dir).glob(point_pattern))
        track_files = list(Path(track_dir).glob(track_pattern))

        if point_files and track_files:
            batch_files.append(BatchFile(
                batch_num=batch_num,
                label=label,
                raw_file=os.path.join(iq_dir, raw_file),
                point_file=str(point_files[0]),
                track_file=str(track_files[0])
            ))
        else:
            missing_point = len(point_files) == 0
            missing_track = len(track_files) == 0
            msg = f"警告：批号 {batch_num}、标签 {label} 的"
            if missing_point and missing_track:
                msg += "点迹和航迹文件均未找到，已跳过。"
            elif missing_point:
                msg += "点迹文件未找到，已跳过。"
            else:
                msg += "航迹文件未找到，已跳过。"
            print(msg)

    if not batch_files:
        raise ValueError("未找到符合命名规则的批量处理文件（需为：航迹批号_Label_目标类型标签.dat）！")

    return batch_files


def split_train_val(data_root: str, num_classes, val_ratio=0.2, shuffle=True):
    label_nums = [0 for _ in range(num_classes)]
    batch_files_by_cls = [[] for _ in range(num_classes)]
    batch_files = get_batch_file_list(data_root)
    for batch_file in batch_files:
        cls = batch_file.label - 1
        if cls < 0 or cls >= num_classes:
            continue
        label_nums[cls] += 1
        batch_files_by_cls[cls].append(batch_file)
    train_nums = [int(num * (1 - val_ratio)) for num in label_nums]
    val_nums = [num - train_num for num, train_num in zip(label_nums, train_nums)]
    train_batch_files, val_batch_files = [], []
    for i, batch_file in enumerate(batch_files_by_cls):
        if shuffle:
            random.shuffle(batch_file)
        train_batch_files.extend(batch_file[:train_nums[i]])
        val_batch_files.extend(batch_file[train_nums[i]:train_nums[i] + val_nums[i]])
    return train_batch_files, val_batch_files


class RDSeq(Dataset):
    def __init__(self, batch_files: list[BatchFile], transform=None, seq_len=180):
        super().__init__()
        self.batch_files = batch_files
        self.transform = transform
        self.seq_len = seq_len

    def __len__(self):
        return len(self.batch_files)

    def __getitem__(self, item):
        batch_file = self.batch_files[item]
        cls = batch_file.label - 1
        # load rd map
        images = self._process_batch(batch_file)
        image_mask = np.ones((self.seq_len,), dtype=np.int32)
        if images.shape[0] < self.seq_len:
            images = np.concatenate([images, np.zeros((self.seq_len - images.shape[0], *images.shape[1:]))], axis=0)
            image_mask[images.shape[0]:] = 0
        elif images.shape[0] > self.seq_len:
            indices = np.linspace(0, images.shape[0] - 1, self.seq_len, dtype=int)
            images = images[indices]
        assert images.shape[0] == self.seq_len

        return images, image_mask, cls

    def _process_batch(self, batch: BatchFile):
        """处理单个批次的数据"""
        # 打开原始数据文件
        frame_count = 0
        rd_matrices = []
        try:
            with open(batch.raw_file, 'rb') as fid:
                while True:
                    params, data = read_raw_data(fid)
                    if params is None or data is None:
                        break

                    frame_count += 1

                    # 跳过没有航迹信息的帧
                    if len(params.track_no_info) == 0:
                        continue

                    # 添加数据验证
                    if len(params.track_no_info) < 4:
                        continue

                    # 验证参数有效性
                    if params.prt <= 0 or params.prt_num <= 0 or params.freq <= 0:
                        continue

                    try:
                        # MTD处理
                        distance_bins = data.shape[0]  # 距离单元数 (31)
                        prt_bins = data.shape[1]  # PRT数

                        # 生成泰勒窗 - 使用PRT数作为窗长，匹配MATLAB
                        mtd_win = signal.windows.taylor(prt_bins, nbar=4, sll=30)

                        # 在距离维度重复窗函数
                        coef_mtd_2d = np.tile(mtd_win, (distance_bins, 1))

                        # 加窗处理
                        data_windowed = data * coef_mtd_2d

                        # FFT处理 - 在PRT维度（轴1）进行FFT
                        mtd_result = fftshift(fft(data_windowed, axis=1), axes=1)

                        # 计算多普勒速度轴 - 修复溢出问题
                        try:
                            delta_v = C / (2 * params.prt_num * params.prt * params.freq)

                            # 检查delta_v是否有效
                            if not np.isfinite(delta_v) or delta_v <= 0 or delta_v > 10000:
                                print(f"警告：帧 {frame_count} delta_v异常: {delta_v}, 跳过该帧")
                                continue

                            # 修复溢出问题 - 使用更安全的方式
                            half_prt = params.prt_num // 2

                            # 检查half_prt是否合理
                            if half_prt <= 0 or half_prt > 10000:
                                print(f"警告：帧 {frame_count} half_prt异常: {half_prt}, 跳过该帧")
                                continue

                            # 使用int32避免溢出
                            v_start = -int(half_prt)
                            v_end = int(half_prt)
                            v_indices = np.arange(v_start, v_end, dtype=np.int32)
                            v_axis = v_indices.astype(np.float64) * delta_v

                            # 检查v_axis是否有效
                            if not np.all(np.isfinite(v_axis)) or len(v_axis) != params.prt_num:
                                print(
                                    f"警告：帧 {frame_count} v_axis异常，长度:{len(v_axis)}, 期望:{params.prt_num}, 跳过该帧")
                                continue

                        except Exception as e:
                            print(f"警告：帧 {frame_count} 计算速度轴时出错: {str(e)}")
                            continue

                        # 目标检测
                        amp_max_vr_unit = int(params.track_no_info[3])

                        # 修正多普勒索引
                        if amp_max_vr_unit > half_prt:
                            amp_max_vr_unit = amp_max_vr_unit - half_prt
                        else:
                            amp_max_vr_unit = amp_max_vr_unit + half_prt

                        # 转换为Python的0-based索引
                        amp_max_vr_unit = amp_max_vr_unit - 1

                        # 确保索引在有效范围内
                        amp_max_vr_unit = np.clip(amp_max_vr_unit, 0, params.prt_num - 1)

                        # 目标中心位于第16个距离单元
                        center_local_bin = 15
                        local_radius = 5

                        # 计算局部检测窗口
                        range_start_local = max(0, center_local_bin - local_radius)
                        range_end_local = min(mtd_result.shape[0], center_local_bin + local_radius + 1)
                        doppler_start = max(0, amp_max_vr_unit - local_radius)
                        doppler_end = min(mtd_result.shape[1], amp_max_vr_unit + local_radius + 1)

                        target_sig = mtd_result[range_start_local:range_end_local, doppler_start:doppler_end]

                        # 检测峰值
                        abs_target = np.abs(target_sig)
                        if abs_target.size == 0:
                            continue

                        max_idx = np.unravel_index(np.argmax(abs_target), abs_target.shape)
                        amp_max_index_row, amp_max_index_col = max_idx

                        # 获取目标全局距离单元索引
                        global_range_bin = int(params.track_no_info[2])

                        # 计算实际距离范围
                        range_start_bin = global_range_bin - 15
                        range_end_bin = global_range_bin + 15

                        # 计算真实距离轴
                        range_plot = np.arange(range_start_bin, range_end_bin + 1) * DELTA_R

                        # 转换到全局距离位置
                        detected_range_bin = range_start_local + amp_max_index_row
                        if detected_range_bin >= len(range_plot):
                            continue

                        # 安全地计算多普勒速度
                        doppler_idx = doppler_start + amp_max_index_col
                        if doppler_idx >= len(v_axis):
                            continue

                        # 保存MTD处理结果
                        rd_matrix = mtd_result
                        velocity_axis = v_axis
                        velocity_mask = np.reshape(np.abs(velocity_axis) < 56, -1)
                        rd_matrix = rd_matrix[:, velocity_mask]
                        rd_matrix = np.abs(rd_matrix)
                        velocity_index = np.where(np.reshape(velocity_axis, -1) == 0)[0][0]
                        rd_matrix[:, velocity_index - 4:velocity_index + 3] = 0
                        rd_matrix[rd_matrix < np.percentile(rd_matrix, 5)] = 0
                        rd_matrix = rd_matrix[:, :, None]
                        if self.transform:
                            rd_matrix = self.transform(rd_matrix)
                        rd_matrices.append(rd_matrix)

                    except Exception as e:
                        # 静默跳过有问题的帧，避免过多错误输出
                        continue

        except Exception as e:
            raise ValueError(f"读取原始数据文件失败：{str(e)}")

        rd_matrices = np.stack(rd_matrices, axis=0)
        return rd_matrices
