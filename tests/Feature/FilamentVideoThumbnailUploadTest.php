<?php

namespace Tests\Feature;

use App\Filament\Resources\VideoResource\Pages\EditVideo;
use App\Models\User;
use App\Models\Video;
use Illuminate\Foundation\Testing\RefreshDatabase;
use Illuminate\Http\UploadedFile;
use Illuminate\Support\Facades\Storage;
use Livewire\Livewire;
use Tests\TestCase;

class FilamentVideoThumbnailUploadTest extends TestCase
{
    use RefreshDatabase;

    public function test_admin_can_upload_image_thumbnail_to_existing_video(): void
    {
        Storage::fake('public');
        Storage::fake('local');

        $admin = User::factory()->create(['role' => 'admin']);
        $video = Video::factory()->create([
            'user_id' => $admin->id,
            'video_url' => 'videos/existing.mp4',
            'thumbnail_url' => null,
        ]);
        Storage::disk('public')->put('videos/existing.mp4', 'video');

        $this->actingAs($admin);

        // Real 1x1 PNG so the test doesn't need the GD extension.
        $png = base64_decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==');

        Livewire::test(EditVideo::class, ['record' => $video->getRouteKey()])
            ->set('data.thumbnail_url', [UploadedFile::fake()->createWithContent('thumb.png', $png)])
            ->assertHasNoErrors()
            ->call('save')
            ->assertHasNoErrors();

        $thumbnail = $video->fresh()->thumbnail_url;
        $this->assertNotNull($thumbnail);
        $this->assertStringStartsWith('thumbnails/', $thumbnail);
        Storage::disk('public')->assertExists($thumbnail);
    }
}
