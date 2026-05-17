import requests
from bs4 import BeautifulSoup
import json

# Fetch Bloomberg homepage
url = "https://www.bloomberg.com"
headers = {
    'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
}

try:
    response = requests.get(url, headers=headers, timeout=30)
    response.raise_for_status()
    
    # Parse HTML
    soup = BeautifulSoup(response.text, 'html.parser')
    
    # Find headline elements
    headlines = []
    for h2 in soup.find_all('h2', class_='headline'):
        text = h2.get_text(strip=True)
        if text and len(text) > 10:  # Filter out short headlines
            headlines.append(text)
    
    # Also try alternative selectors
    if not headlines:
        for h3 in soup.find_all('h3', class_='headline'):
            text = h3.get_text(strip=True)
            if text and len(text) > 10:
                headlines.append(text)
    
    # Limit to top 10 headlines
    top_news = headlines[:10]
    
    print(json.dumps(top_news, indent=2))
    
except Exception as e:
    print(f"Error: {e}")
