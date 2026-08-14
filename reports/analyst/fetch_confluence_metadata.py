#!/usr/bin/env python3
"""
Fetch Confluence page history (created date + creator) via bulk REST API.
Output: data/confluence_raw_metadata.json
"""
import json
import os
import sys
import urllib.request
import urllib.parse
from datetime import datetime

# Add scripts directory to path to import paths module
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import paths
from paths import DATA_DIR

BASE_URL = paths.confluence()["base_url"]
SPACE_KEY = paths.confluence()["space"]
OUTPUT_FILE = os.path.join(DATA_DIR, "confluence_raw_metadata.json")

def log(msg: str):
    """Log to stderr"""
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", file=sys.stderr)
    sys.stderr.flush()

def read_auth_header() -> str:
    """Read Confluence auth from environment"""
    # Try environment first
    pat = os.environ.get("CONFLUENCE_PAT") or os.environ.get("CONFLUENCE_PERSONAL_TOKEN")
    if pat:
        return f"Bearer {pat}"
    
    return None

def api_request(endpoint: str, params: dict = None) -> dict:
    """Make authenticated request to Confluence REST API"""
    auth_header = read_auth_header()
    if not auth_header:
        raise RuntimeError("No Confluence authentication found")
    
    url = f"{BASE_URL}{endpoint}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": auth_header,
            "Accept": "application/json",
            "Content-Type": "application/json"
        }
    )
    
    try:
        with urllib.request.urlopen(req, timeout=60) as response:
            return json.loads(response.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        log(f"HTTP Error {e.code}: {e.reason}")
        raise
    except Exception as e:
        log(f"Request failed: {e}")
        raise

def get_all_space_pages(space_key: str, limit: int = 100) -> list:
    """Fetch all page IDs from a space with pagination"""
    all_pages = []
    start = 0
    
    log(f"Fetching pages from {BASE_URL} space={space_key}...")
    
    while True:
        params = {
            "spaceKey": space_key,
            "type": "page",
            "status": "any",
            "start": start,
            "limit": limit,
            "expand": "version,history"
        }
        
        result = api_request("/rest/api/content", params)
        pages = result.get('results', [])
        
        if not pages:
            break
        
        all_pages.extend(pages)
        log(f"  Retrieved {len(pages)} pages (total: {len(all_pages)})")
        
        if len(pages) < limit:
            break
        
        start += limit
        
        if len(all_pages) > 2000:
            log("Warning: Reached safety limit of 2000 pages")
            break
    
    log(f"Total pages found: {len(all_pages)}")
    return all_pages

def extract_metadata(pages: list) -> list:
    """Extract metadata from page list"""
    metadata_list = []
    
    log("Extracting metadata from pages...")
    
    for i, page in enumerate(pages):
        try:
            page_id = str(page.get("id", ""))
            title = page.get("title", f"Page {page_id}")
            
            history = page.get("history", {})
            created = history.get("createdDate", "")
            created_by = history.get("createdBy", {})
            author_created_name = created_by.get("displayName", "")
            
            version = page.get("version", {})
            updated = version.get("when", "")
            author_info = version.get("by", {})
            author_updated_name = author_info.get("displayName", "")
            
            if not author_created_name:
                author_created_name = author_updated_name or "Unknown"
            if not author_updated_name:
                author_updated_name = author_created_name
            
            metadata = {
                "title": title,
                "page_id": page_id,
                "created": created,
                "updated": updated,
                "author_created": author_created_name,
                "author_updated": author_updated_name
            }
            metadata_list.append(metadata)
            
            if (i + 1) % 100 == 0:
                log(f"  Processed {i + 1}/{len(pages)} pages")
                
        except Exception as e:
            log(f"  Error processing page {page.get('id')}: {e}")
    
    return metadata_list

def main():
    log("=" * 60)
    log(f"Confluence {SPACE_KEY} Space - Metadata Collection")
    log("=" * 60)
    
    auth_header = read_auth_header()
    if not auth_header:
        log("ERROR: No authentication found!")
        log("Please set CONFLUENCE_PAT in environment")
        return 1
    
    log("Authentication: OK")
    
    log(f"\nStep 1: Fetching all page IDs from {SPACE_KEY} space...")
    try:
        pages = get_all_space_pages(SPACE_KEY, limit=100)
    except Exception as e:
        log(f"ERROR: Failed to fetch pages: {e}")
        return 1
    
    if not pages:
        log(f"ERROR: No pages found in {SPACE_KEY} space")
        return 1
    
    log("\nStep 2: Extracting metadata...")
    metadata_list = extract_metadata(pages)
    
    output = {
        "total_count": len(metadata_list),
        "space_key": SPACE_KEY,
        "collection_timestamp": datetime.now().isoformat(),
        "pages": metadata_list
    }
    
    os.makedirs(DATA_DIR, exist_ok=True)
    
    log(f"\nStep 3: Writing output to {OUTPUT_FILE}...")
    with open(OUTPUT_FILE, 'w', encoding='utf-8') as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    
    log("=" * 60)
    log(f"SUCCESS: Collected metadata for {len(metadata_list)} pages")
    log(f"Output written to: {OUTPUT_FILE}")
    log("=" * 60)
    
    return 0

if __name__ == "__main__":
    sys.exit(main())