import urllib.parse
import xml.etree.ElementTree as ET
from curl_cffi import requests
from scrapers.base import BaseScraper, RawLead

class GoogleNewsFundingScraper(BaseScraper):
    source_name = "Google News (Funding Signals)"

    def fetch(self) -> list[RawLead]:
        leads = []
        seen_urls = set() # Used to deduplicate articles found in both searches
        
        # 1. Base Parameters (Funding Events + Marketing Distress Signals)
        triggers = '(intitle:"Series A" OR intitle:"Seed" OR intitle:"raises" OR intitle:"raised" OR intitle:"funding" OR "ad costs" OR "CAC" OR "customer acquisition" OR "ad spend")'
        niches = '("SaaS" OR "DTC" OR "creator economy" OR "skincare")'
        
        # 2. The Broad Net (Open Internet)
        broad_query = f'{triggers} AND {niches}'
        queries = [broad_query]
        
        # 3. The Verified Platforms (Batched into groups of 3 to prevent RSS query degradation)
        verified_sites = [
            "finsmes.com", "siliconangle.com", "techfundingnews.com", "businesswire.com",
            "news.crunchbase.com", "prnewswire.com", "vcnewsdaily.com", "pitchbook.com",
            "fintechfutures.com", "vestbee.com", "growthlist.co", "fundup.ai",
            "fundraiseinsider.com", "fundtq.com", "scouts.yutori.com"
        ]
        
        for i in range(0, len(verified_sites), 3):
            batch = verified_sites[i:i+3]
            sites_str = " OR ".join([f"site:{site}" for site in batch])
            queries.append(f'({sites_str}) AND {triggers} AND {niches}')
        
        # Execute all queries (1 broad + 5 batched verified)
        for query in queries:
            safe_query = urllib.parse.quote(query)
            
            # Constrained to the last 24 hours
            url = f"https://news.google.com/rss/search?q={safe_query}+when:1d&hl=en-US&gl=US&ceid=US:en"
            
            try:
                # Using safari17_0 for stealth as it has higher success rates generally
                res = requests.get(url, impersonate="safari17_0", timeout=15)
                if res.status_code != 200:
                    print(f"[{self.source_name}] Blocked with status: {res.status_code}")
                    continue
                    
                root = ET.fromstring(res.text)
                
                # Cap at 15 items per sweep to prevent local hardware bottlenecks
                for item in root.findall('.//item')[:15]:
                    link = item.find('link').text if item.find('link') is not None else ""
                    
                    # Deduplication: Skip if we already scraped this article in the broad sweep
                    if link in seen_urls:
                        continue
                        
                    title = item.find('title').text if item.find('title') is not None else ""
                    description = item.find('description').text if item.find('description') is not None else ""
                    
                    full_text = f"Headline: {title}\nSummary: {description}"
                    
                    leads.append(RawLead(
                        source=self.source_name,
                        raw_text=full_text,
                        url=link
                    ))
                    
                    seen_urls.add(link)
                    
            except Exception as e:
                print(f"[{self.source_name}] Error parsing XML: {e}")
                
        return leads
