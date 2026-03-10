# dot_to_image.py
import sys
from graphviz import Source

def dot_to_image(dot_file_path, output_file_path, output_format='png'):
    """
    将 DOT 文件渲染为图片
    
    参数:
        dot_file_path (str): 输入的 DOT 文件路径
        output_file_path (str): 输出图片路径（不含扩展名）
        output_format (str): 输出图片格式，例如 'png', 'pdf', 'svg'
    """
    try:
        with open(dot_file_path, 'r', encoding='utf-8') as f:
            dot_content = f.read()
        
        src = Source(dot_content)
        src.format = output_format
        output_path = src.render(filename=output_file_path, cleanup=True)
        print(f"[+] 图片生成成功: {output_path}")
    except Exception as e:
        print(f"[!] 图片生成失败: {e}")

if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("用法: python dot_to_image.py <input.dot> <output_image_name> [format]")
        print("示例: python dot_to_image.py spark_esg.dot spark_esg png")
        sys.exit(1)
    print("123")
    dot_file = sys.argv[1]
    output_name = sys.argv[2]
    fmt = sys.argv[3] if len(sys.argv) > 3 else 'png'
    
    dot_to_image(dot_file, output_name, fmt)