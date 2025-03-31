from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from app.utils.ai import request_kol_recent_data
import logging

logger = logging.getLogger(__name__)

scheduler = AsyncIOScheduler()
is_initialized = False

async def update_kol_data():
    """Background task to update KOL data cache"""
    try:
        logger.info("Starting scheduled KOL data update")
        await request_kol_recent_data()
        logger.info("Completed scheduled KOL data update")
    except Exception as e:
        logger.error(f"Error updating KOL data: {str(e)}")

def init_scheduler():
    """Initialize the scheduler with tasks"""
    global is_initialized
    if is_initialized:
        logger.info("Scheduler already initialized, skipping...")
        return
        
    if not scheduler.running:
        scheduler.add_job(
            update_kol_data,
            trigger=IntervalTrigger(minutes=60),
            id='update_kol_data',
            name='Update KOL data cache',
            replace_existing=True
        )
        scheduler.start()
        is_initialized = True
        logger.info("Scheduler initialized")