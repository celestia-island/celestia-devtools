// Copyright (c) 2026 Celestia Island. All rights reserved.

#include <windows.h>

int create_handle(HANDLE handle)
{
    return static_cast<int>(reinterpret_cast<std::uintptr_t>(handle));
}
