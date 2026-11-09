<?php

namespace App\Http\Requests\Auth;

use App\SocialAuthenticationProvider;
use Illuminate\Foundation\Http\FormRequest;
use Illuminate\Validation\Rule;

class SocialLoginRequest extends FormRequest
{
    /**
     * Determine if the user is authorized to make this request.
     */
    public function authorize(): bool
    {
        return false;
    }

    /**
     * Get the validation rules that apply to the request.
     *
     * @return array<string, \Illuminate\Contracts\Validation\ValidationRule|array<mixed>|string>
     */
    public function rules(): array
    {
        return [
            'id_token' => ['required', 'string'],
            'provider' => ['required', Rule::enum(SocialAuthenticationProvider::class)],
            'name'     => ['nullable', 'string', 'max:255'],
        ];
    }
}
