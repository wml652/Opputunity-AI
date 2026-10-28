import asyncio, sys, json, re
import asyncpg
url = [l.split("=",1)[1].strip() for l in open("D:/2.0/backend/.env") if l.startswith("DATABASE_MIGRATION_URL")][0].replace("+asyncpg","")
async def main():
    c = await asyncpg.connect(url)
    for q in sys.argv[1:]:
        rows = await c.fetch(q)
        for r in rows:
            print(json.dumps({k:(str(v) if not isinstance(v,(int,float,type(None),bool,list,dict)) else v) for k,v in dict(r).items()}, default=str))
        print("---", len(rows))
    await c.close()
asyncio.run(main())
