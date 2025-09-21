#!/usr/bin/env python3
"""
双机器狗控制系统主程序
整合所有功能模块，提供完整的双机器狗控制解决方案
"""

import os
import sys
import logging
import argparse
from datetime import datetime
from pathlib import Path

# 添加项目路径到sys.path
project_root = Path(__file__).parent
sys.path.insert(0, str(project_root))

from dual_dog_gui import DualDogGUI


class DualDogLogger:
    """双机器狗控制系统日志管理器"""
    
    def __init__(self, log_dir: str = "logs", log_level: str = "INFO"):
        self.log_dir = Path(log_dir)
        self.log_level = getattr(logging, log_level.upper())
        self._setup_log_directory()
        self._setup_logger()
    
    def _setup_log_directory(self):
        """创建日志目录"""
        self.log_dir.mkdir(exist_ok=True)
        
        # 创建不同类型的日志文件
        self.main_log_file = self.log_dir / f"dual_dog_main_{datetime.now().strftime('%Y%m%d')}.log"
        self.error_log_file = self.log_dir / f"dual_dog_errors_{datetime.now().strftime('%Y%m%d')}.log"
        self.movement_log_file = self.log_dir / f"dual_dog_movement_{datetime.now().strftime('%Y%m%d')}.log"
    
    def _setup_logger(self):
        """设置日志记录器"""
        # 创建根日志记录器
        root_logger = logging.getLogger()
        root_logger.setLevel(logging.DEBUG)
        
        # 清除已有的处理器
        for handler in root_logger.handlers[:]:
            root_logger.removeHandler(handler)
        
        # 创建格式化器
        detailed_formatter = logging.Formatter(
            '%(asctime)s - %(name)s - %(levelname)s - %(filename)s:%(lineno)d - %(message)s'
        )
        simple_formatter = logging.Formatter(
            '%(asctime)s - %(levelname)s - %(message)s'
        )
        
        # 控制台处理器
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setLevel(self.log_level)
        console_handler.setFormatter(simple_formatter)
        root_logger.addHandler(console_handler)
        
        # 主日志文件处理器
        main_file_handler = logging.FileHandler(self.main_log_file, encoding='utf-8')
        main_file_handler.setLevel(logging.DEBUG)
        main_file_handler.setFormatter(detailed_formatter)
        root_logger.addHandler(main_file_handler)
        
        # 错误日志文件处理器
        error_file_handler = logging.FileHandler(self.error_log_file, encoding='utf-8')
        error_file_handler.setLevel(logging.ERROR)
        error_file_handler.setFormatter(detailed_formatter)
        root_logger.addHandler(error_file_handler)
        
        # 为特定模块创建专门的日志记录器
        movement_logger = logging.getLogger("DualDogMovement")
        movement_file_handler = logging.FileHandler(self.movement_log_file, encoding='utf-8')
        movement_file_handler.setLevel(logging.DEBUG)
        movement_file_handler.setFormatter(detailed_formatter)
        movement_logger.addHandler(movement_file_handler)
        
        # 记录日志系统初始化信息
        logging.info("=" * 60)
        logging.info("双机器狗控制系统启动")
        logging.info(f"日志目录: {self.log_dir.absolute()}")
        logging.info(f"日志级别: {logging.getLevelName(self.log_level)}")
        logging.info(f"主日志文件: {self.main_log_file}")
        logging.info(f"错误日志文件: {self.error_log_file}")
        logging.info(f"运动日志文件: {self.movement_log_file}")
        logging.info("=" * 60)
    
    def cleanup_old_logs(self, days_to_keep: int = 7):
        """清理旧日志文件"""
        try:
            current_time = datetime.now()
            for log_file in self.log_dir.glob("*.log"):
                file_time = datetime.fromtimestamp(log_file.stat().st_mtime)
                if (current_time - file_time).days > days_to_keep:
                    log_file.unlink()
                    logging.info(f"删除旧日志文件: {log_file}")
        except Exception as e:
            logging.error(f"清理旧日志文件失败: {e}")


def create_desktop_shortcut():
    """创建桌面快捷方式（仅在Linux下）"""
    try:
        import os
        desktop_path = Path.home() / "Desktop"
        if not desktop_path.exists():
            desktop_path = Path.home() / "桌面"  # 中文桌面
        
        if desktop_path.exists():
            shortcut_content = f"""[Desktop Entry]
Version=1.0
Type=Application
Name=双机器狗控制系统
Comment=Unitree Go2双机器狗控制系统
Exec=python3 {project_root / 'dual_dog_main.py'}
Icon={project_root / 'icon.png'}
Terminal=false
Categories=Application;
"""
            
            shortcut_file = desktop_path / "dual_dog_control.desktop"
            with open(shortcut_file, 'w', encoding='utf-8') as f:
                f.write(shortcut_content)
            
            # 设置可执行权限
            os.chmod(shortcut_file, 0o755)
            
            logging.info(f"桌面快捷方式已创建: {shortcut_file}")
            return True
    except Exception as e:
        logging.warning(f"创建桌面快捷方式失败: {e}")
    
    return False


def check_dependencies():
    """检查依赖项"""
    missing_deps = []
    
    # 检查必需的Python模块
    required_modules = [
        'tkinter',
        'asyncio',
        'threading',
        'logging',
        'json',
        'time',
        'typing',
        'dataclasses',
        'enum',
        'pathlib'
    ]
    
    for module in required_modules:
        try:
            __import__(module)
        except ImportError:
            missing_deps.append(module)
    
    # 检查项目特定模块
    try:
        from go2_webrtc_driver.webrtc_driver import Go2WebRTCConnection
        from go2_webrtc_driver.constants import RTC_TOPIC, SPORT_CMD
    except ImportError as e:
        logging.error(f"无法导入Go2 WebRTC驱动模块: {e}")
        missing_deps.append("go2_webrtc_driver")
    
    if missing_deps:
        logging.error(f"缺少依赖项: {missing_deps}")
        return False
    
    logging.info("所有依赖项检查通过")
    return True


def print_system_info():
    """打印系统信息"""
    import platform
    import sys
    
    logging.info("系统信息:")
    logging.info(f"  操作系统: {platform.system()} {platform.release()}")
    logging.info(f"  Python版本: {sys.version}")
    logging.info(f"  项目路径: {project_root}")
    logging.info(f"  工作目录: {os.getcwd()}")


def main():
    """主函数"""
    parser = argparse.ArgumentParser(description="双机器狗控制系统")
    parser.add_argument("--log-level", choices=["DEBUG", "INFO", "WARNING", "ERROR"], 
                       default="INFO", help="日志级别")
    parser.add_argument("--log-dir", default="logs", help="日志目录路径")
    parser.add_argument("--create-shortcut", action="store_true", help="创建桌面快捷方式")
    parser.add_argument("--cleanup-logs", type=int, metavar="DAYS", 
                       help="清理N天前的旧日志文件")
    parser.add_argument("--no-gui", action="store_true", help="不启动GUI（仅用于测试）")
    
    args = parser.parse_args()
    
    # 设置日志系统
    logger_manager = DualDogLogger(args.log_dir, args.log_level)
    
    # 清理旧日志
    if args.cleanup_logs:
        logger_manager.cleanup_old_logs(args.cleanup_logs)
    
    # 打印系统信息
    print_system_info()
    
    # 检查依赖项
    if not check_dependencies():
        logging.error("依赖项检查失败，程序退出")
        sys.exit(1)
    
    # 创建桌面快捷方式
    if args.create_shortcut:
        create_desktop_shortcut()
    
    # 启动GUI
    if not args.no_gui:
        try:
            logging.info("启动双机器狗控制系统GUI")
            app = DualDogGUI()
            app.run()
        except KeyboardInterrupt:
            logging.info("用户中断程序")
        except Exception as e:
            logging.error(f"程序运行出错: {e}", exc_info=True)
        finally:
            logging.info("双机器狗控制系统已退出")
    else:
        logging.info("测试模式：依赖项检查完成，程序退出")


if __name__ == "__main__":
    main()