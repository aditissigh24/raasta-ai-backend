"""
Background task manager for webhook processing.
Ensures one active task per user with automatic cancellation.
"""
import asyncio
import logging
from typing import Dict, Optional, Coroutine, Any
from config.redis_client import redis_client

logger = logging.getLogger(__name__)


# In-memory task registry (user_id -> asyncio.Task)
user_tasks: Dict[str, asyncio.Task] = {}


async def cancel_existing_task(user_id: str) -> bool:
    """
    Cancel existing task for a user if one exists.
    
    Args:
        user_id: User identifier
        
    Returns:
        True if task was cancelled, False if no task existed
    """
    existing_task = user_tasks.get(user_id)
    
    if existing_task is None:
        return False
    
    if existing_task.done():
        # Task already completed, remove from registry
        user_tasks.pop(user_id, None)
        return False
    
    logger.warning(f"⚠️ Cancelling existing task for user {user_id}")
    
    # Cancel the task
    existing_task.cancel()
    
    # Wait for cancellation with timeout
    try:
        await asyncio.wait_for(existing_task, timeout=5.0)
    except asyncio.CancelledError:
        logger.info(f"✅ Task cancelled successfully for user {user_id}")
    except asyncio.TimeoutError:
        logger.warning(f"⚠️ Task cancellation timeout for user {user_id}, forcing cleanup")
    except Exception as e:
        logger.error(f"❌ Error during task cancellation for user {user_id}: {e}")
    
    # Clean up from registry
    user_tasks.pop(user_id, None)
    
    return True


async def acquire_redis_lock(user_id: str, ttl: int = 300) -> bool:
    """
    Acquire Redis lock for a user's task.
    
    Args:
        user_id: User identifier
        ttl: Time-to-live for the lock in seconds (default: 300s = 5 min)
        
    Returns:
        True if lock acquired, False otherwise
    """
    redis_key = f"raasta:webhook:task:{user_id}"
    
    success = await redis_client.set(redis_key, "running", ex=ttl)
    
    if success:
        logger.debug(f"🔒 Redis lock acquired for user {user_id} (TTL: {ttl}s)")
    else:
        logger.debug(f"⚠️ Redis lock failed for user {user_id} (Redis unavailable)")
    
    return success


async def release_redis_lock(user_id: str) -> bool:
    """
    Release Redis lock for a user's task.
    
    Args:
        user_id: User identifier
        
    Returns:
        True if lock released, False otherwise
    """
    redis_key = f"raasta:webhook:task:{user_id}"
    
    success = await redis_client.delete(redis_key)
    
    if success:
        logger.debug(f"🔓 Redis lock released for user {user_id}")
    else:
        logger.debug(f"⚠️ Redis lock release failed for user {user_id} (Redis unavailable)")
    
    return success


def create_cleanup_callback(user_id: str) -> callable:
    """
    Create a cleanup callback for task completion.
    
    Args:
        user_id: User identifier
        
    Returns:
        Callback function to be attached to the task
    """
    def _cleanup(task: asyncio.Task):
        """Cleanup function called when task completes."""
        # Remove from in-memory registry
        user_tasks.pop(user_id, None)
        
        # Release Redis lock asynchronously
        asyncio.create_task(release_redis_lock(user_id))
        
        # Log task completion status
        if task.cancelled():
            logger.info(f"🚫 Task cancelled and cleaned up for user {user_id}")
        elif task.exception():
            logger.error(f"❌ Task failed for user {user_id}: {task.exception()}")
        else:
            logger.info(f"✅ Task completed successfully for user {user_id}")
    
    return _cleanup


async def create_background_task(
    user_id: str,
    coro: Coroutine[Any, Any, Any],
    task_name: Optional[str] = None
) -> asyncio.Task:
    """
    Create a background task with automatic cancellation and cleanup.
    
    This function:
    1. Cancels any existing task for the user
    2. Acquires Redis lock (with TTL for safety)
    3. Creates new background task
    4. Registers cleanup callback
    
    Args:
        user_id: User identifier (deduplication key)
        coro: Coroutine to execute in background
        task_name: Optional name for the task (for debugging)
        
    Returns:
        The created asyncio.Task
    """
    # Step 1: Cancel existing task if present
    await cancel_existing_task(user_id)
    
    # Step 2: Acquire Redis lock (for coordination and observability)
    from config.settings import settings
    await acquire_redis_lock(user_id, ttl=settings.REDIS_TASK_TTL)
    
    # Step 3: Create new background task
    if task_name is None:
        task_name = f"webhook-{user_id}"
    
    task = asyncio.create_task(coro, name=task_name)
    
    # Step 4: Register in memory
    user_tasks[user_id] = task
    
    # Step 5: Attach cleanup callback
    task.add_done_callback(create_cleanup_callback(user_id))
    
    logger.info(f"📋 Created background task for user {user_id}")
    
    return task


async def cancel_all_tasks():
    """
    Cancel all running tasks.
    
    This should be called during application shutdown.
    """
    if not user_tasks:
        logger.info("No active tasks to cancel")
        return
    
    logger.info(f"🛑 Cancelling {len(user_tasks)} active tasks...")
    
    # Create list of tasks to cancel (avoid dict size change during iteration)
    tasks_to_cancel = list(user_tasks.values())
    
    # Cancel all tasks
    for task in tasks_to_cancel:
        if not task.done():
            task.cancel()
    
    # Wait for all tasks to complete (with timeout)
    if tasks_to_cancel:
        try:
            await asyncio.wait_for(
                asyncio.gather(*tasks_to_cancel, return_exceptions=True),
                timeout=10.0
            )
            logger.info("✅ All tasks cancelled successfully")
        except asyncio.TimeoutError:
            logger.warning("⚠️ Some tasks did not cancel within timeout")
    
    # Clear registry
    user_tasks.clear()


def get_active_task_count() -> int:
    """
    Get count of active tasks.
    
    Returns:
        Number of active tasks
    """
    return len(user_tasks)


def get_active_users() -> list[str]:
    """
    Get list of users with active tasks.
    
    Returns:
        List of user IDs with active tasks
    """
    return list(user_tasks.keys())




