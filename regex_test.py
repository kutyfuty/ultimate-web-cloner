import re
from urllib.parse import urljoin

css_content = 'background-image: image-set(url("https://api.zlot.com/sportsbook__static/Assets/Images/0114c80f6c40a6a32263.webp") 1x, url("https://api.zlot.com/sportsbook__static/Assets/Images/66b.webp") 2x)'

def replace_url(match):
    url_value = match.group(1).strip("'\" ")
    print("Found URL:", url_value)
    abs_url = urljoin("https://zlot.com/tr-tr/", url_value)
    
    # Simulate _find_local_path returning a mock path
    local = "_assets/images/0114c80f6c40a6a32263.webp"
    
    # simulate what asset_manager does:
    return f"url('{local}')"

res = re.sub(r"url\(([^)]+)\)", replace_url, css_content)
print("\nReplaced CSS:")
print(res)
