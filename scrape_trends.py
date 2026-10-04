import json
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime

# Headers required to avoid 403 / 429 status codes from Reddit and Google
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}

def fetch_rss(url):
    """Fetch raw XML data from an RSS or Atom feed URL."""
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req) as response:
        return response.read()

def parse_reddit_trends():
    """Fetch and parse Reddit popular feed (Atom format)."""
    reddit_url = "https://www.reddit.com/r/popular/.rss"
    trends = []
    try:
        xml_data = fetch_rss(reddit_url)
        root = ET.fromstring(xml_data)
        
        # Atom feeds use namespace
        ns = {"atom": "http://www.w3.org/2005/Atom"}
        entries = root.findall("atom:entry", ns)
        
        for entry in entries[:15]:
            title_elem = entry.find("atom:title", ns)
            if title_elem is not None and title_elem.text:
                topic = title_elem.text.strip()
                # Create Reddit search link for the topic
                search_url = f"https://www.reddit.com/search/?q={urllib.parse.quote(topic)}"
                trends.append({
                    "title": topic,
                    "search_url": search_url
                })
    except Exception as e:
        print(f"Error fetching Reddit RSS: {e}")
    
    return trends

def parse_google_trends():
    """Fetch and parse Google Daily Search Trends RSS feed."""
    google_url = "https://trends.google.com/trending/rss?geo=US"
    trends = []
    try:
        xml_data = fetch_rss(google_url)
        root = ET.fromstring(xml_data)
        
        # Standard RSS 2.0 format
        items = root.findall(".//item")
        
        for item in items[:15]:
            title_elem = item.find("title")
            if title_elem is not None and title_elem.text:
                topic = title_elem.text.strip()
                # Create Google search link for the topic
                search_url = f"https://www.google.com/search?q={urllib.parse.quote(topic)}"
                trends.append({
                    "title": topic,
                    "search_url": search_url
                })
    except Exception as e:
        print(f"Error fetching Google Trends RSS: {e}")
        
    return trends

def main():
    reddit_trends = parse_reddit_trends()
    google_trends = parse_google_trends()
    
    output_data = {
        "updated_at": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC"),
        "reddit": reddit_trends,
        "google_trends": google_trends
    }
    
    with open("trends.json", "w", encoding="utf-8") as f:
        json.dump(output_data, f, indent=2, ensure_ascii=False)
        
    print(f"Successfully updated trends.json with {len(reddit_trends)} Reddit items and {len(google_trends)} Google Trends items.")

if __name__ == "__main__":
    main()
