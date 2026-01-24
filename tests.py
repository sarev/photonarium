#!/usr/bin/env python3

"""
Automated test suite for the Imaginary image database.

This module provides comprehensive tests for all major functionality
of the image database system. Run directly to execute tests:

    python tests.py

The test suite:
- Creates temporary test data (images, database, config)
- Exercises all major features
- Reports pass/fail results
- Cleans up after itself
"""

from __future__ import annotations

from pathlib import Path
from PIL import Image

import logging
import queue
import random
import shutil
import tempfile
import traceback
import uuid

# Import everything we need to test from imagedb
from imagedb import (
    ImageDatabase,
    EventQueue,
    create_image,
    get_image_by_path,
    extract_image_metadata,
    compute_all_duplicate_groups,
    restore_image,
)
from config import load_config


def run_tests() -> None:
    """Run automated tests of all main functionality.

    Creates temporary test data, exercises all major features, and
    reports results. Cleans up after itself.
    """

    # Configure logging for test output
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(message)s',
        datefmt='%H:%M:%S',
    )

    print('\n' + '=' * 70)
    print('IMAGINARY DATABASE - AUTOMATED TEST SUITE')
    print('=' * 70 + '\n')

    # Create temporary directory for test
    test_dir = Path(tempfile.mkdtemp(prefix='imaginary_test_'))
    db_path = test_dir / 'test.db'
    thumbnail_dir = test_dir / 'thumbnails'
    config_path = test_dir / 'config.yml'
    image_dir = test_dir / 'images'
    image_dir.mkdir()

    print(f'Test directory: {test_dir}\n')

    passed = 0
    failed = 0

    def test(name: str, condition: bool, detail: str = '') -> bool:
        nonlocal passed, failed
        if condition:
            print(f'  [PASS] {name}')
            passed += 1
            return True
        else:
            print(f'  [FAIL] {name}')
            if detail:
                print(f'         {detail}')
            failed += 1
            return False

    try:
        # =====================================================================
        # Test 1: Create test images
        # =====================================================================
        print('1. Creating test images...')

        test_images = []

        # Create some test images with different characteristics
        colors = [
            ('red', (255, 0, 0)),
            ('green', (0, 255, 0)),
            ('blue', (0, 0, 255)),
            ('yellow', (255, 255, 0)),
            ('purple', (128, 0, 128)),
        ]

        for name, color in colors:
            # Create a simple colored image
            img = Image.new('RGB', (200, 150), color)

            # Add some variation (random noise) to make them slightly different
            pixels = img.load()
            for _ in range(100):
                x, y = random.randint(0, 199), random.randint(0, 149)
                r, g, b = pixels[x, y]
                pixels[x, y] = (
                    min(255, r + random.randint(-10, 10)),
                    min(255, g + random.randint(-10, 10)),
                    min(255, b + random.randint(-10, 10)),
                )

            path = image_dir / f'{name}.jpg'
            img.save(path, 'JPEG', quality=90)
            test_images.append(path)

        # Create a duplicate (exact copy)
        shutil.copy(test_images[0], image_dir / 'red_copy.jpg')
        test_images.append(image_dir / 'red_copy.jpg')

        # Create a near-duplicate (slightly modified)
        img = Image.open(test_images[1])
        img = img.resize((180, 135))  # Slightly different size
        path = image_dir / 'green_similar.jpg'
        img.save(path, 'JPEG', quality=85)
        test_images.append(path)

        # Create a PNG (lossless) image
        img = Image.new('RGBA', (100, 100), (0, 128, 255, 200))
        path = image_dir / 'blue_alpha.png'
        img.save(path, 'PNG')
        test_images.append(path)

        test(f'Created {len(test_images)} test images', len(test_images) == 8)

        # =====================================================================
        # Test 2: Configuration
        # =====================================================================
        print('\n2. Testing configuration...')

        config = load_config(config_path)
        test('Config created with defaults', config_path.exists())
        test('Config has correct batch size', config.embedding_batch_size == 16)
        test('Config has image extensions', '.jpg' in config.image_extensions)

        # =====================================================================
        # Test 3: Database initialization
        # =====================================================================
        print('\n3. Testing database initialization...')

        # Create database without auto-start (we'll test components separately)
        db = ImageDatabase(
            db_path=db_path,
            thumbnail_dir=thumbnail_dir,
            config_path=config_path,
            auto_start=False,
        )

        test('Database file created', db_path.exists())
        test('Database connection open', db.conn is not None)
        test('Thumbnail directory created', thumbnail_dir.exists())

        # =====================================================================
        # Test 4: Folder management
        # =====================================================================
        print('\n4. Testing folder management...')

        # Add folder
        result = db.add_folder(str(image_dir))
        test('Add folder returns result', result is not None)
        test('Add folder has correct path', result and result['path'] == str(image_dir))

        # List folders
        folders = db.get_folders()
        test('Get folders returns list', len(folders) == 1)
        test('Folder path correct', folders[0]['path'] == str(image_dir))

        # Add same folder again (should return None)
        result2 = db.add_folder(str(image_dir))
        test('Adding duplicate folder returns None', result2 is None)

        # =====================================================================
        # Test 5: Image ingestion (manual, without threads)
        # =====================================================================
        print('\n5. Testing image ingestion...')

        # Manually ingest images
        for img_path in test_images:
            metadata = extract_image_metadata(img_path)
            if metadata:
                image_id = str(uuid.uuid4())
                create_image(
                    db.conn,
                    image_id=image_id,
                    path=metadata.path,
                    size=metadata.size,
                    width=metadata.width,
                    height=metadata.height,
                    timestamp=metadata.timestamp,
                    checksum=metadata.checksum,
                    perceptual_hash=metadata.perceptual_hash,
                    laplacian_var=metadata.laplacian_var,
                    lossless=metadata.lossless,
                )

        images = db.get_all_images()
        test(f'Ingested {len(images)} images', len(images) == len(test_images))

        # Check image properties
        if images:
            img = images[0]
            test('Image has ID', 'id' in img and img['id'])
            test('Image has path', 'path' in img and img['path'])
            test('Image has dimensions', img.get('width', 0) > 0 and img.get('height', 0) > 0)
            test('Image has checksum', 'checksum' in img and img['checksum'])

        # =====================================================================
        # Test 6: Image queries
        # =====================================================================
        print('\n6. Testing image queries...')

        if images:
            # Get single image
            img = db.get_image(images[0]['id'])
            test('Get image by ID', img is not None)

            # Get by path
            img_by_path = get_image_by_path(db.conn, images[0]['path'])
            test('Get image by path', img_by_path is not None)

            # Update image
            updated = db.update_image(images[0]['id'], {
                'description': 'Test description',
                'rating': '⭐⭐⭐',
            })
            test('Update image returns result', updated is not None)
            test('Description updated', updated and updated.get('description') == 'Test description')
            test('Rating updated', updated and updated.get('rating') == '⭐⭐⭐')

        # =====================================================================
        # Test 7: Thumbnail generation
        # =====================================================================
        print('\n7. Testing thumbnail generation...')

        if images:
            thumb_path = db.get_thumbnail_path(images[0]['id'], size=100)
            test('Thumbnail generated', thumb_path is not None and thumb_path.exists())

            if thumb_path and thumb_path.exists():
                thumb_img = Image.open(thumb_path)
                test('Thumbnail has correct max dimension',
                     max(thumb_img.size) <= 100)

        # =====================================================================
        # Test 8: Duplicate detection
        # =====================================================================
        print('\n8. Testing duplicate detection...')

        # Compute duplicates
        results = compute_all_duplicate_groups(db.conn, db.config)
        test('Duplicate computation completed', results is not None)
        test('Level 0 (identical) found duplicates', results.get(0, 0) >= 1,
             f'Found {results.get(0, 0)} groups')

        # Get duplicate groups
        level0_groups = db.get_duplicate_groups(0)
        test('Get duplicate groups returns list', isinstance(level0_groups, list))

        if level0_groups:
            group = level0_groups[0]
            test('Group has images', 'images' in group and len(group['images']) >= 2)

        # =====================================================================
        # Test 9: Stats and status
        # =====================================================================
        print('\n9. Testing stats and status...')

        stats = db.get_stats()
        test('Stats has totalImages', 'totalImages' in stats)
        test('Stats has totalFolders', 'totalFolders' in stats)
        test('Total images correct', stats['totalImages'] == len(test_images))

        status = db.get_processing_status()
        test('Status has status field', 'status' in status)
        test('Status has queue counts', 'indexing_queue' in status and 'embedding_queue' in status)

        # =====================================================================
        # Test 10: Event queue
        # =====================================================================
        print('\n10. Testing event queue...')

        # Create event queue and emit events
        eq = EventQueue()
        subscriber = eq.subscribe()
        test('Subscriber created', subscriber is not None)

        eq.emit('test_event', {'message': 'hello'})
        try:
            event = subscriber.get(timeout=1.0)
            test('Event received', event is not None)
            test('Event type correct', event.event_type == 'test_event')
            test('Event data correct', event.data.get('message') == 'hello')
        except queue.Empty:
            test('Event received', False, 'Queue was empty')

        eq.unsubscribe(subscriber)
        test('Unsubscribe works', eq.subscriber_count == 0)

        # =====================================================================
        # Test 11: Soft delete and restore
        # =====================================================================
        print('\n11. Testing soft delete and restore...')

        if images:
            image_id = images[-1]['id']

            # Soft delete
            deleted = db.delete_image(image_id, from_disk=False)
            test('Soft delete returns True', deleted)

            # Image should not appear in normal query
            visible_images = db.get_all_images(include_deleted=False)
            test('Deleted image hidden', len(visible_images) == len(images) - 1)

            # But should appear with include_deleted
            all_images = db.get_all_images(include_deleted=True)
            test('Deleted image in full list', len(all_images) == len(images))

            # Restore
            restored = restore_image(db.conn, image_id)
            test('Restore returns True', restored)

            visible_after = db.get_all_images(include_deleted=False)
            test('Restored image visible', len(visible_after) == len(images))

        # =====================================================================
        # Test 12: Folder removal
        # =====================================================================
        print('\n12. Testing folder removal...')

        removed = db.remove_folder(str(image_dir))
        test('Remove folder returns True', removed)

        folders_after = db.get_folders()
        test('No folders remaining', len(folders_after) == 0)

        # Images should be marked as deleted
        visible_after_remove = db.get_all_images(include_deleted=False)
        test('Images marked deleted after folder removal', len(visible_after_remove) == 0)

        # =====================================================================
        # Test 13: Context manager
        # =====================================================================
        print('\n13. Testing context manager...')

        db.close()
        test('Database closed', db.is_closed)

        # Re-open with context manager
        with ImageDatabase(
            db_path=db_path,
            thumbnail_dir=thumbnail_dir,
            config_path=config_path,
            auto_start=False,
        ) as db2:
            test('Context manager enters', not db2.is_closed)
            stats2 = db2.get_stats()
            test('Database accessible in context', stats2 is not None)

        test('Context manager exits and closes', db2.is_closed)

        # =====================================================================
        # Summary
        # =====================================================================
        print('\n' + '=' * 70)
        print(f'TEST RESULTS: {passed} passed, {failed} failed')
        print('=' * 70)

        if failed == 0:
            print('\nAll tests passed!')
        else:
            print(f'\n{failed} test(s) failed.')

    except Exception as e:
        print(f'\n[ERROR] Test suite crashed: {e}')
        traceback.print_exc()
        failed += 1

    finally:
        # Cleanup
        print(f'\nCleaning up test directory: {test_dir}')
        try:
            shutil.rmtree(test_dir)
            print('Cleanup complete.')
        except Exception as e:
            print(f'Warning: Could not fully clean up: {e}')

    # Exit with appropriate code
    raise SystemExit(0 if failed == 0 else 1)


if __name__ == '__main__':
    run_tests()
